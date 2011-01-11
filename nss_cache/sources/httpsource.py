#!/usr/bin/python
#
# Copyright 2007 Google Inc.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation,
# Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

"""An implementation of an http data source for nsscache."""

__author__ = ('blaedd@google.com (David MacKinnon',)

import bz2
import calendar
import cStringIO
import logging
import os
import pycurl
import time
import urlparse

from nss_cache import error
from nss_cache import maps
from nss_cache.sources import base
from nss_cache.util import files
from nss_cache.util import curl


class HttpFilesSource(base.Source):
  """Source for data fetched via HTTP."""
  # HTTP defaults
  PASSWD_URL = ''
  SHADOW_URL = ''
  GROUP_URL = ''
  AUTOMOUNT_BASE_URL = ''
  NETGROUP_URL = ''
  RETRY_DELAY = 5
  RETRY_MAX = 3
  TLS_CACERTFILE = '/etc/ssl/certs/ca-certificates.crt'

  # for registration
  name = 'http'

  def __init__(self, conf, conn=None):
    """Initialise the HTTP Data Source.

    Args:
      conf: config.Config instance
      conn: pycurl Curl object
    """
    super(HttpFilesSource, self).__init__(conf)
    self._SetDefaults(conf)
    if not conn:
      conn = pycurl.Curl()
      conn.setopt(pycurl.NOPROGRESS, 1)
      conn.setopt(pycurl.NOSIGNAL, 1)
      conn.setopt(pycurl.USERAGENT, 'nsscache')

    self.conn = conn

  def _SetDefaults(self, configuration):
    """Set defaults if necessary."""
    if not 'automount_base_url' in configuration:
      configuration['automount_base_url'] = self.AUTOMOUNT_BASE_URL
    if not 'passwd_url' in configuration:
      configuration['passwd_url'] = self.PASSWD_URL
    if not 'shadow_url' in configuration:
      configuration['shadow_url'] = self.SHADOW_URL
    if not 'group_url' in configuration:
      configuration['group_url'] = self.GROUP_URL
    if not 'netgroup_url' in configuration:
      configuration['netgroup_url'] = self.GROUP_URL
    if not 'retry_delay' in configuration:
      configuration['retry_delay'] = self.RETRY_DELAY
    if not 'retry_max' in configuration:
      configuration['retry_max'] = self.RETRY_MAX
    if not 'tls_cacertfile' in configuration:
      configuration['tls_cacertfile'] = self.TLS_CACERTFILE

  def GetPasswdMap(self, since=None):
    """Return the passwd map from this source.

    Args:
      since: Get data only changed since this timestamp (inclusive) or None
      for all data.

    Returns:
      instance of maps.PasswdMap
    """
    return PasswdUpdateGetter().GetUpdates(self, self.conf['passwd_url'], since)

  def GetShadowMap(self, since=None):
    """Return the shadow map from this source.

    Args:
      since: Get data only changed since this timestamp (inclusive) or None
      for all data.

    Returns:
      instance of maps.ShadowMap
    """
    return ShadowUpdateGetter().GetUpdates(self, self.conf['shadow_url'],
                                           since)

  def GetGroupMap(self, since=None):
    """Return the group map from this source.

    Args:
      since: Get data only changed since this timestamp (inclusive) or None
      for all data.

    Returns:
      instance of maps.GroupMap
    """
    return GroupUpdateGetter().GetUpdates(self, self.conf['group_url'], since)


  def GetNetgroupMap(self, since=None):
    """Return the netgroup map from this source.

    Args:
      since: Get data only changed since this timestamp (inclusive) or None
      for all data.

    Returns:
      instance of maps.NetgroupMap
    """
    return NetgroupUpdateGetter().GetUpdates(self, self.conf['netgroup_url'],
                                             since)

  def GetAutomountMap(self, since=None, location=None):
    """Return an automount map from this source.

    Note that autmount maps are stored in multiple locations, thus we expect
    a caller to provide a location.  We also follow the automount spec and
    set our search scope to be 'one'.

    Args:
      location: Currently a string containing our search base, later we
        may support hostname and additional parameters.
      since: Get data only changed since this timestamp (inclusive) or None
        for all data.

    Returns:
      instance of AutomountMap

    Raises:
      EmptyMap:
    """
    if location is None:
      self.log.error('A location is required to retrieve an automount map!')
      raise error.EmptyMap
    automount_url = urlparse.urljoin(self.conf['automount_base_url'],
                                     location)
    return AutomountUpdateGetter().GetUpdates(self, automount_url, since)


  def GetAutomountMasterMap(self):
    """Return the autmount master map from this source.

    Returns:
      an instance of maps.AutomountMap
    """
    master_map = self.GetAutomountMap(location='auto.master')
    for map_entry in master_map:
      map_entry.location = os.path.split(map_entry.location)[1]
      self.log.debug('master map has: %s' % map_entry.location)
    return master_map


class UpdateGetter(object):
  """Base class that gets updates over http."""

  def __init__(self):
    self.log = logging.getLogger(self.__class__.__name__)

  def FromTimestampToHttp(self, ts):
    """Converts internal nss_cache timestamp to HTTP timestamp.

    Args:
      ts: number of seconds since epoch
    Returns:
      HTTP format timestamp string
    """
    ts = time.gmtime(ts)
    return time.strftime('%a, %d %b %Y %H:%M:%S GMT', ts)

  def FromHttpToTimestamp(self, http_ts_string):
    """Converts HTTP timestamp string to internal nss_cache timestamp.

    Args:
      HTTP format timestamp string
    Returns:
      number of seconds since epoch
    """
    t = time.strptime(http_ts_string, '%a, %d %b %Y %H:%M:%S GMT')
    return calendar.timegm(t)

  def GetUpdates(self, source, url, since):
    """Get updates from a source.

    Args:
      source: A data source
      url: url to the data we want
      since: a timestamp representing the last change (None to force-get)

    Returns:
      A tuple containing the map of updates and a maximum timestamp

    Raises:
      ValueError: an object in the source map is malformed
      ConfigurationError:
    """
    proto = url.split(':')[0]
    # Newer libcurl allow you to disable protocols there. Unfortunately
    # it's not in dapper or hardy.

    if proto not in ('http', 'https'):
      raise error.ConfigurationError('Unsupported protocol %s' % proto)

    conn = source.conn
    conn.setopt(pycurl.OPT_FILETIME, 1)
    conn.setopt(pycurl.ENCODING, 'bzip2, gzip')
    if since is not None:
      conn.setopt(pycurl.TIMEVALUE, since)
      conn.setopt(pycurl.TIMECONDITION, pycurl.TIMECONDITION_IFMODSINCE)

    retry_count = 0
    resp_code = 500
    while retry_count < source.conf['retry_max']:
      try:
        source.log.debug('fetching %s', url)
        (resp_code, headers, body) = curl.CurlFetch(url, conn, self.log)
        self.log.debug('response code: %s', resp_code)
      finally:
        if resp_code < 400:
          # Not modified-since
          if resp_code == 304:
            return []
          if resp_code == 200:
            break
        retry_count += 1
        self.log.warning('Failed connection: attempt #%s.', retry_count)
        if retry_count == source.conf['retry_max']:
          self.log.debug('max retries hit')
          raise error.SourceUnavailable('Max retries exceeded.')
        time.sleep(source.conf['retry_delay'])

    headers = headers.split('\r\n')
    last_modified = conn.getinfo(pycurl.INFO_FILETIME)
    self.log.debug('last modified: %s', last_modified)
    if last_modified == -1:
      for header in headers:
        if header.lower().startswith('last-modified'):
          self.log.debug('%s', header)
          http_ts_string = header[header.find(':')+1:].strip()
          last_modified = self.FromHttpToTimestamp(http_ts_string)
    else:
      http_ts_string = self.FromTimestampToHttp(last_modified)

    self.log.debug('Last-modified is: %s', http_ts_string)

    # curl (on Ubuntu hardy at least) will handle gzip, but not bzip2
    try:
      response = cStringIO.StringIO(bz2.decompress(body))
      self.log.debug('bzip encoding found')
    except IOError:
      response = cStringIO.StringIO(body)

    data_map = self.GetMap(cache_info=response)
    if http_ts_string:
      http_ts = self.FromHttpToTimestamp(http_ts_string)
      self.log.debug('setting last modified to: %s', http_ts)
      data_map.SetModifyTimestamp(http_ts)

    return data_map

  def GetParser(self):
    """Return the appropriate parser. Must be implemented by child class."""
    raise NotImplementedError

  def GetMap(self, cache_info):
    """Creates a Map from the cache_info data.

    Args:
      cache_info: file-like object containing the data to parse

    Returns:
      A child of Map containing the cache data.
    """
    return self.GetParser().GetMap(cache_info, self.CreateMap())


class AutomountUpdateGetter(UpdateGetter):
  """Get automount updates."""

  def GetParser(self):
    """Returns a MapParser to parse FilesAutomount cache."""
    return files.FilesAutomountMapParser()

  def CreateMap(self):
    """Returns a new AutomountMap instance."""
    return maps.AutomountMap()


class PasswdUpdateGetter(UpdateGetter):
  """Get passwd updates."""

  def GetParser(self):
    """Returns a MapParser to parse FilesPasswd cache."""
    return files.FilesPasswdMapParser()

  def CreateMap(self):
    """Returns a new PasswdMap instance to have PasswdMapEntries added to it."""
    return maps.PasswdMap()


class ShadowUpdateGetter(UpdateGetter):
  """Get shadow updates."""

  def GetParser(self):
    """Returns a MapParser to parse FilesShadow cache."""
    return files.FilesShadowMapParser()

  def CreateMap(self):
    """Returns a new ShadowMap instance to have ShadowMapEntries added to it."""
    return maps.ShadowMap()


class GroupUpdateGetter(UpdateGetter):
  """Get group updates."""

  def GetParser(self):
    """Returns a MapParser to parse FilesGroup cache."""
    return files.FilesGroupMapParser()

  def CreateMap(self):
    """Returns a new GroupMap instance to have GroupMapEntries added to it."""
    return maps.GroupMap()

class NetgroupUpdateGetter(UpdateGetter):
  """Get netgroup updates."""

  def GetParser(self):
    """Returns a MapParser to parse FilesNetgroup cache."""
    return files.FilesNetgroupMapParser()

  def CreateMap(self):
    """Returns a new NetgroupMap instance to have GroupMapEntries added to it."""
    return maps.NetgroupMap()


# Finally, register the Source
base.RegisterImplementation(HttpFilesSource)