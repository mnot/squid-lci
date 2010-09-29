Linked Cache Invalidation (LCI) for the Squid Cache

Copyright (c) 2008-2010 Yahoo! Inc. 
See src/lci_manager.py for license.

What is LCI?
============

LCI is a Squid helper that allows incoming requests to invalidate related
cached responses.

There are two ways that this can happen.

1. Cached responses can be related to invalidating URIs by linking to them with
   a special response header. When the invalidating URI is POSTed, PUT or
   DELETEd to, it will trigger the related responses to be considered stale.

   In this mode, the invalidating response doesn't need to know about the URIs 
   to be invalidated, and

2. A POST, PUT or DELETE response can contain a header that directly invalidates
   one or more related resources by their URIs.

In either case, the invalidation will be propagated to peered caches, so that
they can invalidate their contents as well.

Generally, this all happens in a very small number of milliseconds, unlike
other solutions in this space; thus "near-realtime."

Example: Blog Postings
----------------------

If your site has User Generated Content, LCI can help to keep contents
up-to-date. Every time a user posts to a blog::

  POST /users/bob/new-article HTTP/1.1
  Host: example.yahoo.com
  Content-Type: text/example

  here is my new blog entry...

it will change a number of things on your system, including the blog itself,
entry counts, user posting metadata, and so forth. If you include a link to
Bob's new-article resource in each of those responses::

  Link: </users/bob/new-article>; rel="invalidated-by"

it will invalidate those things every time Bob posts a new blog article.

LCI can accommodate many such links, so that a single POST, PUT or DELETE can
invalidate a few hundred cached responses. In some cases, you may want to
further simplify things, by giving a "synthetic" URI to these responses::

  Link: <http://fake.yahoo.com/placeholder/1>; rel="invalidated-by"

So that in a future POST, PUT or DELETE responses, you can invalidate things
arbitrarily::

  HTTP/1.1 201 Created
  Location: /over/there
  Link: <http://fake.yahoo.com/placeholder/1>; rel="invalidates"

Example: Search Service
-----------------------

Imagine that you provide a movie search service, and use caching for
scalability. If a movie star's information changes (e.g., Nicole Kidman having
a baby), you want that information to be reflected in all views of your data as
soon as possible; however, because it's a search service, there are many
different URIs that could be in-cache::

   http://example.yahoo.com/movie-search?term=Nicole
   http://example.yahoo.com/movie-search?term=Kidman
   http://example.yahoo.com/movie-search?term=Nicole+Kidman
   http://example.yahoo.com/movie-search?term=Kidmann
   http://example.yahoo.com/movie-search?term=Australia+Kidman
   ...

If you tell LCI that these are all related to the Nicole Kidman resource::

  Link: </movie-stars/Nicole+Kidman>; rel="invalidated-by"

then all of the cached search responses will be invalidated when you change
Nicole's details::

  PUT /movies-stars/Nicole+Kidman HTTP/1.1
  Host: example.yahoo-inc.com
  Content-Type: text/example

  Nicole had a baby!


Using LCI
=========

Prerequisites
-------------

LCI Requires:
  * Squid 2-HEAD <http://www.squid-cache.org/Versions/v2/HEAD/>
    Note that Squid 3 is not yet able to support LCI (because it doesn't
    implement the log daemon facility). Squid 2.7 does not yet have
    patches to allow invalidation and HTCP CLRs. Hopefully, Squid 2.8 will
    be released soon.
  * Python 2.5 or greater <http://www.python.org/>
  * Twisted <http://twistedmatrix.com/>

LCI Configuration
-----------------

You must create a LCI configuration file; sample.conf is an example of this.
The following configuration lines MUST be set:
  - dbfile: where to keep the LCI manager's state.
  - logfile: where to keep the LCI manager's log.

Both locations should be writable to the Squid user.

Make sure that the lci_manager script has the correct location of the Python
interpreter, and adjust as necessary.

Squid Configuration
-------------------

To use LCI, you must configure Squid to fork it as a log daemon, as well as
a cache peer.

Because of limitations in Squid currently, you MUST also:
  * disable any other logfile daemons (Squid can only run one currently), and
  * turn off collapsed forwarding (because of an incompatibility in the code).

An example Squid configuration snippet::

  # Set up the manager as a logfile daemon.
  acl lci_responses rep_header Link -i invalidate
  acl hier_none hier_code NONE
  logformat lci_format %rm %rU %#{Link}<h %#{Age}<h %#{Cache-Control}<h
  logfile_daemon /path/to/lci_manager.py /path/to/lci_manager.conf
  access_log daemon:/path/to/lci_manager.py lci_format lci_responses !hier_none

  # configure the manager as a cache peer, and send CLRs to it appropriately.
  cache_peer localhost sibling 7 $(htcp_port) htcp htcp-only-clr
    htcp-no-purge-clr htcp-forward-clr no-digest no-query name=lci_peer
  cache_peer_access lci_peer deny all
  htcp_clr_access allow localhost

  # Allow the manager to sent PURGE requests
  acl PURGE method PURGE
  http_access allow PURGE localhost

NOTE: the cache_peer line has been wrapped for formatting here.

After adding this to your Squid configuration (making changes to paths as
necessary), restart your squid process; e.g::

  > sudo /usr/local/squid -k shutdown
  [wait]
  > sudo /usr/local/squid

The Squid process should now have forked the lci_manager.py process; you can
verify this in a number of ways;
- Verifying the process exists (e.g., with top or pstree)
- Checking Squid's cache.log (look for lines with 'daemon' in them)
- Checking LCI's logfile (as configured).

The LCI manager will now be operational. If you have problems, check
both Squid's cache.log as well as the LCI log for error messages.

Frequently Asked Questions
==========================

Is LCI transactional?
  No. Because of both how it is implemented, and because multiple hosts are
  involved, it is not possible to guarantee that a request already in progress
  (or one received shortly afterwards) will honour an invalidation event.
  However, in most cases, the "gap" is quite small; on the order of a small
  number of milliseconds (<10) in-colo, and only that much plus the one-way
  latency inter-colo.

  If your client needs to have a response that reflects the changes they've
  just made immediately (e.g., when POSTing a new blog, showing the updated
  blog page in the response), the best thing to do is to return the updated
  information in the response to the change for immediate use.

Is LCI reliable?
  No; there are cases where invalidation events may not be applied to all
  caches containing a copy of the target response. For example, the network
  between the two caches could be down, HTCP CLRs could be lost (since it is
  a UDP-based protocol) or one of the caches could be down for maintenance.
  Some attempts are made to correct for some kinds of temporary outages, and
  during normal operation these kinds of failures won't be seen.

  As a result, LCI is well-suited for deployments where these kinds of failures
  are acceptable, as long as they are infrequent. If you're looking for an
  invalidation mechanism with a higher degree of reliability, see
  Cache Channels.

What's the difference between LCI and Cache Channels?
  Cache Channels are designed to give freshness control over a large number of
  inter-related responses reliably, with the trade-off being that it's
  relatively slow. LCI notifies caches much more quickly, but is not as
  reliable, and cannot scale to as large a number of responses as Channels can.

If A invalidates B and B invalidates C, will A invalidate C?
  Not at this time; LCI events are currently single-hop.

How many groups can one response be associated with?
  There is a practical limit to the number of groups a response can be
  associated with. Because of HTTP header size limits and implementation
  concerns, it may not be feasible to associate more than approximately 50-100
  groups with a single response.

Does LCI consume resources on the Squid server?
  A little bit. Although it should not noticeably affect request latency or
  overall capacity of the intermediary (because it is not in the critical
  path for processing), LCI does consume memory, to store the associations
  between responses and groups. Depending upon usage patterns, Squid may need
  to have less cache_mem configured, and/or the machine may need more memory
  installed. However, this should only be necessary in extreme cases.

Are invalidated responses removed from cache?
  When invalidated, cached responses are considered stale, not truly purged:
  Under certain circumstances, they may still be reused. However, this is
  controllable, using max_stale in Squid configuration, as well as the
  stale-if-error response cache-control directive.

How will caches that don't implement LCI behave?
  Caches in the request chain that do not understand this HTTP extension will 
  not invalidate the associated responses. This is important to understand,
  for example, when your clients may also be caching. Future protocol
  extensions may enable us to avoid this effect.

My URIs have queries that have different equivalent forms. Will LCI work?
  If there is any change (e.g., in case, order of parameters, etc.) in the 
  URI of an event or cached response, the event will not be applied. For
  example, if clients access a resource as both
  http://www.example.com/foo?a=bar&b=baz and
  http://www.example.com/foo?b=baz&a=bar, they are treated as separate URIs.


Background on the Squid <-> LCI Manager Protocol
================================================

The LCI Manager uses HTCP CLR to invalidate associated URLs (collected by
observing the invalidated-by link relation), and HTTP PURGE to invalidate
those URLs that are directly made invalid by the invalidates link relation.

This is because directly invalid URLs need to be communicated to peers, while
doing so for associated URLs isn't necessary (because peers will know these
relationships if they have any relevant cached responses), and would cause
too much chatter on the network (as well as more possibility of loops, etc.).

Thus...

We send HTCP CLRs to the LCI manager when:
 1) we get a POST/PUT/DELETE/etc. from clients for a given URI
 2) we get a HTCP CLR for a peer.
but NOT when we get a PURGE.

We send HTCP CLRs to peers when:
 1) we get a POST/PUT/DELETE/etc. from clients for a given URI
 2) we get a PURGE
but NOT when we get a HTCP CLR.

This implies that regular peers should just be configured with 'htcp',
optionally with 'htcp-only-clr' if desired.


