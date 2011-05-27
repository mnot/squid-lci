#!/usr/bin/env python

"""
LCI Manager
by Mark Nottingham <mnot@yahoo-inc.com>.

It requires Python 2.5+ and the following additional libraries:
 - Twisted <http://twistedmatrix.com/>

The LCI Manager is started by Squid as a log daemon; that is, squid will
write log lines to it on STDIN (see sample.conf for the format of the lines). 
This is handled by an instance of SquidLogProtocol.

Simultaneously, it listens on a UDP port for HTCP CLR messages from Squid, 
which indicate that a POST, PUT or DELETE has invalidated a URI (either 
in the local instance of Squid, or possibly a peer). This is implemented
in the HtcpProtocol class.

Both of these protocols are overseen by an LciManager instance, which keeps
state (as a ManagerState instance, which can be persisted to disk) and, based
upon information from the kept state, HTCP invalidations and log entries, will
send HTTP PURGEs and HTCP CLRs to Squid.

To avoid looping and unnecessary propagation of invalidations, LCI uses the
available information channels in the folowing way:

  * Squid logs are scanned for new "inv-by" link relations, which
    are kept in ManagerState.
  * Squid logs are scanned for new "invalidates" link relations (only on
    requests with unsafe methods); the Manager will invalidate them as well as
    any associated URIs (as per "inv-by" link relations) in Squid.
  * Incoming HTCP CLRs from Squid indicate requests (possibly local, possibly
    from peers) that invalidate a URI; the Manager will find associated
    (as per "inv-by" link relations) URIs in state and invalidate them
    in Squid.
  * The LCI Manager invalidates associated URIs (i.e., with "inv-by")
    by sending HTTP PURGEs directly to Squid. Squid will NOT forward these
    invalidations to its peers.
  * The LCI Manager invalidates directly indicated URIs (i.e., those found
    via "invalidates") using HTCP CLRs to Squid. These invalidations WILL be
    forwarded to its peers.

"""

__copyright__ = """\
Copyright (c) 2008-2010 Yahoo! Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import fcntl
import struct
import sys
import time

import ConfigParser
import cPickle as pickle
import gzip
import hashlib
import logging
import os
import re
import signal
import traceback
from logging.handlers import RotatingFileHandler
from urllib import unquote
from urlparse import urljoin
from xml.parsers import expat

# try to get the epollreactor
try:
    from twisted.internet import epollreactor
    epollreactor.install()
except ImportError:
    pass
from twisted.internet import reactor, stdio
from twisted.protocols.basic import LineReceiver
from twisted.internet.protocol import DatagramProtocol, connectionDone
from twisted.internet import error as internet_error
from twisted.web import client, error as web_error

def main(configfile):        
    # load config
    try:
        config = ConfigParser.SafeConfigParser({
                       'http_proxy': 'localhost:3128',
                       'http_proxy_htcp_port': '4827',
                       'htcp_host': 'localhost',
                       'htcp_port': '4828',
                       'log_level': '%s' % logging.INFO,
                       'log_backup': '5',
                       'purge_timeout': '5',
                       'purge_retry_wait_base': '5',
                       'purge_retry_tries': '8',
                       'gc_ttl_fudge': '5',
                       'gc_ttl_min': '0',
                       'use_gzip': 'false',
        })
        config.read(configfile)
        logfile = config.get("main", "logfile")
        log_level = config.get("main", "log_level").strip().upper()
        log_backup = config.getint("main", "log_backup")
    except ConfigParser.Error, why:
        error("Configuration file: %s\n" % why)
            
    # start logging
    log = logging.getLogger()
    try:
        hdlr = RotatingFileHandler(logfile, backupCount=log_backup)
    except IOError, why:
        error("Can't open log file (%s)" % why)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    hdlr.setFormatter(formatter)
    log.addHandler(hdlr) 
    log.setLevel(logging._levelNames.get(log_level, logging.INFO))
    # run
    try:
        mgr = LciManager(reactor, config, log)
        # we ignore SIGTERM because Squid will close the log FH, which gives
        # us a much cleaner signal that we're to shut down.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        mgr.start()
    except ConfigParser.Error, why:
        error("Configuration file: %s" % why)
    except Exception, why:
        error("Error: %s " % why)
    except:
        error("Unknown error.")
        
    # clean up logging
    hdlr.flush()
    hdlr.close()
    logging.shutdown()

def error(msg):
    "Something really bad has happened. Should only be used during startup."
    logging.critical(msg)
    sys.stderr.write("LCI FATAL: %s\n" % msg)
    sys.exit(1)


############################################################################

class ManagerState:
    "Holds the manager's state in an easily persistable way."
    def __init__(self):
        self.groups = {} # key is hashed group_uri; value is set of req uris
        self.requests = {} # key is request uri; value is set of
            # (hashed group_uris, ttl, timeout_instance)
        self.purges = set() # set of outstanding purge uris

class LciManager:
    "Coordinates the LCI protocol."
    def __init__(self, re_actor, config, log):
        self.reactor = re_actor
        self.config = config
        self.log = log
        self.state = ManagerState()
        self.htcp_handler = HtcpProtocol(self)
        self.log_handler = SquidLogProtocol(self)
        self.shutting_down = False
        self.stat_clr_recv = 0
        self.stat_clr_sent = 0
        self.stat_purge_sent = 0

    def start(self):
        "Start the manager."
        self.log.info("start_manager")
        stdio.StandardIO(self.log_handler)
        htcp_port = self.config.getint("main", "htcp_port")
        self.reactor.listenUDP(port=htcp_port, protocol=self.htcp_handler,
            interface='localhost', maxPacketSize=8192) #IGNORE:E1101
        self.log.info("Listening for HTCP on localhost:%s" % (htcp_port))
        self.load_state()
        self.reactor.run(installSignalHandlers=False)

    def shutdown(self):
        "Stop the manager."
        # TODO: is shutting_down necessary? Check twisted semantics...
        if self.shutting_down or not self.reactor.running:
            return
        self.shutting_down = True
        self.log.info("stop_manager")
        if self.htcp_handler.transport:
            stop_deferred = self.htcp_handler.transport.stopListening()
            if not stop_deferred:
                self.log.info("Stopped listening to HTCP port.")
            else:
                self.log.info("Stopping listen on HTCP port...")
                self.reactor.runUntilCurrent()
        self.save_state()
        self.reactor.stop()

    def load_state(self):
        "Load any persisted state."
        dbfile = self.config.get("main", "dbfile")
        try:
            db_lock = open(dbfile, 'a')
            fcntl.flock(db_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError, why:
            # can't get a lock
            self.log.info("db_read_locked")
            self.reactor.callLater(1, self.load_state)
            return
        start_time = time.time()
        try:
            db = gzip.open(dbfile, 'rb')
            # taste the file to see if it's compressed
            try:
                db.read(1)
                db.seek(0)
            except (IOError, EOFError):
                # open non-gzipped file
                db = open(dbfile, 'rb')
            try:
                saved_state = pickle.load(db)
            except (ValueError, pickle.PickleError), why:
                self.log.error("db_read_corrupt (%s)" % why)
                return
            db.close()
        except (IOError, EOFError), why:
            self.log.warning("db_read_error (%s)" % why)
            return
        finally:
            db_lock.close()

        # remember what URIs we were in the process of purging.
        for purge_uri in saved_state.purges:
            self.purge(purge_uri)
        # reconsitute gc events for request URIs.
        self.state.groups = saved_state.groups.copy()
        now = time.time()
        for request_uri, v in saved_state.requests.items():
            try:
                (group_uri_hashs, exp, timeout) = v
            except ValueError:
                self.log.warning("Old state file format found; ignoring.")
                self.state.groups = {}
                break
            ttl = max(exp - now, 0)
            timeout = self.reactor.callLater(
                ttl, self.forget_groups, request_uri, True
            )
            self.state.requests[request_uri] = (
                group_uri_hashs, ttl + now, timeout
            )
        self.log.info("state_loaded (%3.3f seconds)" % (
            time.time() - start_time
        ))
        self.note_state()

    def save_state(self):
        "Persist manager state."
        self.note_state()
        dbfile = self.config.get("main", "dbfile")
        try:
            db_lock = open(dbfile, 'a')
            fcntl.flock(db_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError, why:
            # can't get a lock
            self.log.info("db_write_locked")
            self.reactor.callLater(1, self.save_state)
            return
        start_time = time.time()
        pruned_requests = dict(
            [(k, (v0, v1, None)) for \
             (k, (v0, v1, v2)) in self.state.requests.items()]
        )
        self.state.requests = pruned_requests
        try:
            if self.config.getboolean('main', 'use_gzip'):
                db = gzip.open(dbfile, 'wb', 6)
            else:
                db = open(dbfile, 'wb')
            try:
                pickle.dump(self.state, db, pickle.HIGHEST_PROTOCOL)
            except pickle.PickleError, why:
                self.log.error("db_write_corrupt (%s)" % why)
            db.close()
        except IOError, why:
            self.log.warning("db_write_error (%s)" % why)
        finally:
            db_lock.close()
        self.log.info("state_saved (%3.3f seconds)" % (
            time.time() - start_time
        ))

    def note_state(self):
        """
        Log periodic stats.
        """
        self.log.info("db_groups: %i, db_requests: %i, db_purges: %i" %
                      (len(self.state.groups),
                       len(self.state.requests),
                       len(self.state.purges))
                     )
        self.log.info("clr_recv: %i, clr_sent: %i, purge_sent: %i" %
                      (self.stat_clr_recv,
                       self.stat_clr_sent,
                       self.stat_purge_sent)
                     )
        self.stat_clr_recv = 0
        self.stat_clr_sent = 0
        self.stat_purge_sent = 0


    def set_groups(self, request_uri, group_uri_list, ttl):
        """
        Remember that an unsafe request to anything in group_uri_list will
        invalidate request_uri. Forget it after ttl.
        """
        request_uri = self._c14n_uri(request_uri)
        group_uri_list = [urljoin(request_uri, u) for u in group_uri_list]
        group_uri_hashs = set([self._hash_uri(u) for u in group_uri_list])
        self.forget_groups(request_uri)
        timeout = self.reactor.callLater(
            ttl, self.forget_groups, request_uri, True
        )
        self.state.requests[request_uri] = (
            group_uri_hashs, 
            ttl + time.time(), 
            timeout
        )
        # remember the new group memberships
        self.log.debug("set_groups: <%s> to %s" % (
            request_uri, group_uri_list
        ))
        for group_uri_hash in group_uri_hashs:
            if self.state.groups.has_key(group_uri_hash):
                self.state.groups[group_uri_hash].add(request_uri)
            else:
                self.state.groups[group_uri_hash] = set([request_uri])

    def forget_groups(self, request_uri, from_timeout=False):
        """
        Forget the associated group_uris for a given request_uri.
        """
        self.log.debug("forget_groups: <%s>" % request_uri)
        for group_uri_hash in self.state.requests.get(request_uri, [[]])[0]:
            if self.state.groups.has_key(group_uri_hash):
                self.state.groups[group_uri_hash].discard(request_uri)
                if not self.state.groups[group_uri_hash]:
                    del self.state.groups[group_uri_hash]
        if not from_timeout:
            # cancel the impending gc_timeout
            try:
                self.state.requests[request_uri][2].cancel()
            except KeyError:
                pass
            except AttributeError:
                self.log.warning("can't cancel timeout for %s." % request_uri)
            except internet_error.AlreadyCancelled:
                self.log.warning(
                    "scheduled event for %s already canceled." % request_uri
                )
            except internet_error.AlreadyCalled:
                self.log.warning(
                    "scheduled event for %s already called." % request_uri
                )
        try:
            del self.state.requests[request_uri]
        except KeyError:
            pass

    empty_set = set()
    def clr_related(self, uri):
        """
        Send a HTCP CLR to our local proxy for each URI that's
        associated with the given URI, according to what we've seen.
        """
        self.log.debug("clr_related: <%s>" % uri)
        for request_uri in self.state.groups.get(
            self._hash_uri(uri), self.empty_set
        ).copy():
            self.htcp_handler.send_CLR(request_uri)
            self.forget_groups(request_uri)

    def _c14n_uri(self, uri):
        """
        Hook for canonicalising a URI if we want to.
        """
        return uri

    def _hash_uri(self, uri):
        """
        Hash the URI for memory efficiency.
        """
        return hashlib.md5(self._c14n_uri(uri)).digest()

    def purge(self, uri, tries_left=None):
        """
        Send a HTTP PURGE request to URI using our local proxy.
        """
        self.log.debug("purging <%s>" % uri)
        self.stat_purge_sent += 1
        if tries_left == None:
            tries_left = self.config.getint("main", "purge_retry_tries")
        def purge_done(data):
            self._purge_done(uri)
        def purge_error(data):
            tl = tries_left - 1
            if data.type == web_error.Error:
                if data.value[0] in ["404", "410"]:
                    self.log.debug("Nothing to purge <%s>" % uri)
                    return self._purge_done(uri)
                else:
                    msg = '"%s"' % data.value
            elif data.type == internet_error.DNSLookupError:
                msg = '"DNS lookup error"'
            elif data.type == internet_error.TimeoutError:
                msg = '"Timeout"'
            elif data.type == internet_error.ConnectionRefusedError:
                msg = '"Connection refused"'
            elif data.type == internet_error.ConnectError:
                msg = '"Connection error"'
            elif data.type == expat.ExpatError:
                msg = '"XML parsing error (%s)"' % data.value
            else:
                msg = '"Unknown error (%s)"' % traceback.format_exc()
            self._purge_error(uri, msg, tl)
        self.state.purges.add(uri)
        c = getPage(str(uri),
                    method="PURGE",
                    timeout=self.config.getint("main", "purge_timeout"),
                    proxy=self.config.get("main", "http_proxy"),
                    #            headers=req_headers
                    )
        c.addCallback(purge_done)
        c.addErrback(purge_error)
                
    def _purge_done(self, uri, msg=""):
        self.log.debug("purge_done <%s> %s" % (uri, msg))
        self.state.purges.discard(uri)

    def _purge_error(self, uri, msg, tries_left):
        self.log.warning("purge_error <%s> (%s); %s tries left" % (
            uri, msg, tries_left))
        if tries_left > 0:
            def retry():
                self.log.info("purge_retry <%s>" % uri)
                self.purge(uri, tries_left)
            factor = self.config.getint("main", "purge_retry_tries") \
                     - tries_left
            when = self.config.getint("main", "purge_retry_wait_base") \
                   ** factor
            self.reactor.callLater(when, retry)
        else:   
            self.log.error("purge_error <%s> (%s); giving up" % (uri, msg))
            self.state.purges.discard(uri)


############################################################################

htcp_header = struct.Struct('!HBBHBBI')
htcp_header_fields = (
                      'length',
                      'major',
                      'minor',
                      'data_length',
                      'op_resp',
                      'f1_rr',
                      'txn_id',
                      )
htcp_opcodes = {
    0:  'nop',
    1:  'tst',
    2:  'mon',
    3:  'set',
    4:  'clr',
}
htcp_clr_reasons = {
    0:  'some reason not better specified by another code',
    1:  'the origin server told me that this entity does not exist',
}

class HtcpProtocol(DatagramProtocol):
    "Partial implementation of the HTCP protocol (RFC2756)."
    def __init__(self, mgr):
        self.mgr = mgr
        self.log = mgr.log
        self.txn_magic = 0

    def datagramReceived(self, data, (host, port)):
        try:
            request = dict(zip(htcp_header_fields,
                           htcp_header.unpack(data[:12])))
            request['from'] = (host, port)
            request['opcode'] = htcp_opcodes[(request['op_resp'] & 0xf0) >> 4]
            request['response'] = request['op_resp'] & 0x0f
            del request['op_resp']
            request['rr'] = request['f1_rr'] & 0x1
            f1 = (request['f1_rr'] & 0x2) >> 1
            if request['rr'] == 0:
                request['rd'] = f1
#                self.log.debug("HTCP Response desired; ignoring")
            else:
                request['mo'] = f1
            del request['f1_rr']
            self.log.debug('HTCP %d.%d %s' % (
                request['major'], 
                request['minor'], 
                request['opcode'].upper()
            ))
        except Exception, why:
            self.log.error("Bad HTCP hdr: <<<%s>>> from %s:%s (%s)" % \
                (str(data[:12]), host, port, why)
            )
            return
        try:
            req_data = HtcpUnpacker(data[12:])
        except Exception, why:
            self.log.error("Bad HTCP packet data: <<<%s>>> from %s:%s (%s)" %
                (str(data[12:]), host, port, why))
            return
        getattr(self, 'do_%s' % request['opcode'], self.unhandled)(
            request, req_data
        )

    def do_clr(self, request, req_data):
        """
        Received a HTCP CLR datagram.
        """
        if request['txn_id'] == self.txn_magic:
            # ignore CLRs from ourselves. This is a bit hacky, yes. 
            return
        try:
            reason = req_data.unpack('!H')[0] & 0xf
            spec = req_data.unpack_specifier()
        except Exception, why:
            self.log.error("Bad HTCP CLR: %s from %s:%s txn_id %s (%s)" % (
              repr(req_data), 
              request['from'][0], 
              request['from'][1], 
              request['txn_id'], 
              why
            ))
            return
#        headers = spec['headers'].split('\r\n')
#        for line in headers:
#            self.log.debug('\t\t' + line)
        self.mgr.stat_clr_recv += 1
        # CLR what we know is related (will not be propagated to peers)
        self.mgr.clr_related(spec['uri'])

    def unhandled(self, request, req_data):
        """
        Received a HTCP datagram that we don't recognise.
        """
        self.log.info("Ignoring HTCP %s message" % request['opcode'])

    def send_CLR(self, uri):
        """
        Send a HTCP CLR datagram to our local proxy.
        """
        method = 'GET'
        version = '1/1'
        headers = ''
        opcode = 4
        response = 0
        f1 = 0
        rr = 0
        clr_reason = 0
        txn_id = self.txn_magic
        htcp_major = 0
        htcp_minor = 1
        # Pack the opcode, response, f1, rr and txn_id
        data = struct.pack("!BBI", 
                            (opcode << 4) + response, 
                            (f1 << 1) + rr, 
                            txn_id
        )
        # Pack the CLR reason
        data = data + struct.pack("!H", clr_reason)
        # Pack the specifier, starting with the method as a countstr
        data = data + struct.pack("!H%ds" % len(method), len(method), method)
        # Pack the URI as a countstr
        data = data + struct.pack("!H%ds" % len(uri), len(uri), uri)
        # Pack the version as a countstr
        data = data + struct.pack("!H%ds" % len(version), 
                                  len(version), 
                                  version
        )
        # Pack the headers as a counstr
        data = data + struct.pack("!H%ds" % len(headers), 
                                  len(headers), 
                                  headers
        )
        # Prepend the HTCP version and the length of the data segment.
        data = struct.pack("!BBH", 
                            htcp_major, 
                            htcp_minor, 
                            len(data) + 2
        ) + data
        # Prepend the total length of the packet.
        data = struct.pack("!H", len(data) + 2) + data
        self.log.debug("Sending CLR: <%s>" % uri)
        self.mgr.stat_clr_sent += 1
        self.transport.write(data, ("127.0.0.1",
            self.mgr.config.getint("main", "http_proxy_htcp_port")))
        
        
class HtcpUnpacker(object):
    "Unpacks HTCP messages."
    def __init__(self, data):
        self.data = data
        self.offset = 0

    def __str__(self):
        return self.data[self.offset:]

    def __repr__(self):
        return "offset %s data <<<%s>>>" % (self.offset, repr(self.data))

    def unpack(self, fmt):
        size = struct.calcsize(fmt)
        data = struct.unpack(fmt, self.data[self.offset:self.offset + size])
        self.offset += size
        return data

    def unpack_countstr(self):
        length = struct.unpack('!H', 
                                self.data[self.offset:self.offset + 2]
        )[0]
        self.offset += 2
        string = struct.unpack('!%ds' % length,
                               self.data[self.offset:self.offset + length]
        )[0]
        self.offset += length
        return string

    def unpack_specifier(self):
        spec = {
            'method': self.unpack_countstr(),
            'uri': self.unpack_countstr(),
            'version': self.unpack_countstr(),
            'headers': self.unpack_countstr(),
        }
        spec['version'] = '%s.%s' % tuple(spec['version'].split('/'))
        return spec


############################################################################

TOKEN = r'(?:[^\(\)<>@,;:\\"/\[\]\?={} \t]+?)'
QUOTED_STRING = r'(?:"(?:\\"|[^"])*")'
PARAMETER = r'(?:%(TOKEN)s(?:=(?:%(TOKEN)s|%(QUOTED_STRING)s))?)' % locals()
LINK = r'<[^>]*>\s*(?:;\s*%(PARAMETER)s?\s*)*' % locals()
COMMA = r'(?:\s*(?:,\s*)+)'
PARAM_SPLIT = r'%s(?=%s|\s*$)' % (PARAMETER, COMMA)
LINK_SPLIT = r'%s(?=%s|\s*$)' % (LINK, COMMA)
param_splitter = re.compile(PARAM_SPLIT)
link_splitter = re.compile(LINK_SPLIT)
def _unquotestring(instr):
    if instr[0] == instr[-1] == '"':
        instr = instr[1:-1]
        instr = re.sub(r'\\(.)', r'\1', instr)
    return instr
def _splitstring(instr, item, split):
    if not instr: 
        return []
    return [h.strip() for h in re.findall(r'%s(?=%s|\s*$)' % (item, split), instr)]


class SquidLogProtocol(LineReceiver):
    "Handles the Squid log deamon protocol in STDIN."
    delimiter = '\n'
    safe_methods = ["GET", "HEAD"]

    def __init__(self, mgr):
        self.mgr = mgr
        self.log = mgr.log
        self.gc_ttl_fudge = mgr.config.getint("main", "gc_ttl_fudge")
        self.gc_ttl_min = mgr.config.getint("main", "gc_ttl_min")
        
    def lineReceived(self, line):
        """
        Process a log line.
        """
        code = line[0]
        if code is 'L': # Log
            line = line[1:].rstrip()
            self.log.debug("log: %s" % line)
            try:
                method, uri, link_str, age_str, cc_str = line.split(None, 4)
            except ValueError:
                self.log.error("Misformatted squid log line received")
            links = self._extract_relations(
                self._parse_link(unquote(link_str))
            )
            if method in self.safe_methods \
            and links.has_key('inv-by'):
                # remember dependencies so we can act on them later
                if age_str == '-':
                    age = 0
                else:
                    try:
                        age = int(age_str)
                    except ValueError:
                        self.log.warning(
                            "Invalid response age (%s) for <%s>; assuming 0" %
                            (age_str, uri)
                        )
                        age = 0
                # TODO: implement complete ageing algorithm 
                # (using date, transit times, etc.)
                cc = self._parse_cc(unquote(cc_str))
                try:
                    max_age = int(cc.get('max-age', 0))
                except ValueError:
                    self.log.warning(
                        "Invalid response max-age (%s) for <%s>; assuming 0" %
                        (cc.get('max-age', '-'), uri)
                    )
                    max_age = 0
                ttl = max(
                    max_age - age + self.gc_ttl_fudge, 
                    self.gc_ttl_fudge, 
                    self.gc_ttl_min
                )
                self.mgr.set_groups(uri, links['inv-by'], ttl)
            elif method not in self.safe_methods:
                if links.has_key("invalidates"):
                    # this response says it invalidates something else
                    r_scheme, r_host, r_port, r_path = client._parse(uri)
                    for invalid_uri in \
                    [urljoin(uri, u) for u in links['invalidates']]:
                        i_scheme, i_host, i_port, i_path = \
                            client._parse(invalid_uri)
                        if i_scheme.lower() != r_scheme.lower() or \
                          i_host.lower() != r_host.lower() or \
                          i_port != r_port:
                            self.log.warning(
                                "Not letting <%s> response invalidate <%s>." %
                                (uri, invalid_uri)
                            )
                            continue
                        # Use PURGE so that the invalidations are 
                        # propagated to other peers as CLRs
                        self.mgr.purge(invalid_uri)
                        # Then invalidate what we know is related with CLR
                        # (which isn't propagated)
                        self.mgr.clr_related(invalid_uri)
        elif code is 'R': # Rotate
            self.mgr.note_state()
            self.log.info("Rotating logs...")
            for hdlr in self.log.handlers:
                try:
                    hdlr.doRollover()
                except AttributeError:
                    pass
        else:
            pass

    def connectionLost(self, reason=connectionDone):
        self.mgr.shutdown()

    @staticmethod
    def _parse_cc(instr, force_list=None):
        "Parse an HTTP Cache-Control header."
        out = {}
        if not instr: 
            return out
        for param in [h.strip() for h in param_splitter.findall(instr)]:
            try:
                attr, value = param.split("=", 1)
                value = _unquotestring(value)
            except ValueError:
                attr = param
                value = True
            attr = attr.lower()
            if force_list and attr in force_list:
                if out.has_key(attr):
                    out[attr].append(value)
                else:
                    out[attr] = [value]
            else:
                out[attr] = value
        return out

    @staticmethod
    def _parse_link(instr):
        "Parse an HTTP Link header."
        out = {}
        if not instr: 
            return out
        for link in [h.strip() for h in link_splitter.findall(instr)]:
            url, params = link.split(">", 1)
            url = url[1:]
            param_dict = {}
            for param in _splitstring(params, PARAMETER, "\s*;\s*"):
                try:
                    a, v = param.split("=", 1)
                    param_dict[a.lower()] = _unquotestring(v)
                except ValueError:
                    param_dict[param.lower()] = None
            out[url] = param_dict
        return out

    @staticmethod
    def _extract_relations(links):
        """
        Given a dictionary of urls and parameters, return
        a dictionary of link relation types with the relevant URLs for values.
        """
        relations = {}
        for url, params in links.items():
            keys = []
            if params.has_key('rel'):
                keys.append(params['rel'])
            for key in keys:
                if relations.has_key(key):
                    relations[key].append(url)
                else:
                    relations[key] = [url]
        return relations

def getPage(url, contextFactory=None, proxy=None, * args, ** kwargs):
    """
    twisted.Web's getPage with proxy support
    """
    scheme, host, port, path = client._parse(url)
    if proxy:
        host, port = proxy.split(':')
        port = int(port)
        kwargs['proxy'] = proxy
    factory = HTTPClientFactory(url, * args, ** kwargs)
    if scheme == 'https':
        from twisted.internet import ssl
        if contextFactory is None:
            contextFactory = ssl.ClientContextFactory()
        reactor.connectSSL(host, port, factory, contextFactory)  #IGNORE:E1101
    else:
        reactor.connectTCP(host, port, factory)  #IGNORE:E1101
    return factory.deferred

class HTTPClientFactory(client.HTTPClientFactory):
    """
    For getPage with proxy support.
    """
    def __init__(self, * args, ** kwargs):
        self.proxy = kwargs.get('proxy', None)
        if self.proxy != None:            
            del kwargs['proxy']
        client.HTTPClientFactory.__init__(self, * args, ** kwargs)
        
    def setURL(self, url):
        self.url = url
        scheme, host, port, path = client._parse(url)
        if scheme and host:
            self.scheme = scheme
            self.host = host
            self.port = port
        if self.proxy:
            self.path = "%s://%s:%s%s" % (self.scheme, 
                                          self.host,
                                          self.port,
                                          path)
        else:
            self.path = path

if __name__ == '__main__':
    try:
        conf = sys.argv[1]
    except IndexError:
        sys.stderr.write("USAGE: %s config_filename\n" % sys.argv[0]) 
        sys.exit(1)
    if not os.path.exists(conf):
        error("Can't find config file %s." % conf)
    main(conf)
