# -*- coding: utf-8 -*-
"""Parser for Java Cache IDX files."""

from __future__ import unicode_literals

# TODO:
#  * 6.02 files did not retain IP addresses. However, the
#    deploy_resource_codebase header field may contain the host IP.
#    This needs to be researched further, as that field may not always
#    be present. 6.02 files will currently return 'Unknown'.

import os

import construct

from dfdatetime import java_time as dfdatetime_java_time

from plaso.containers import events
from plaso.containers import time_events
from plaso.lib import errors
from plaso.lib import definitions
from plaso.lib import timelib
from plaso.parsers import interface
from plaso.parsers import manager


class JavaIDXEventData(events.EventData):
  """Java IDX cache file event data.

  Attributes:
    idx_version (str): format version of IDX file.
    ip_address (str): IP address of the host in the URL.
    url (str): URL of the downloaded file.
  """

  DATA_TYPE = 'java:download:idx'

  def __init__(self):
    """Initializes event data."""
    super(JavaIDXEventData, self).__init__(data_type=self.DATA_TYPE)
    self.idx_version = None
    self.ip_address = None
    self.url = None


class JavaIDXParser(interface.FileObjectParser):
  """Parse Java WebStart Cache IDX files for download events.

  There are five structures defined. 6.02 files had one generic section
  that retained all data. From 6.03, the file went to a multi-section
  format where later sections were optional and had variable-lengths.
  6.03, 6.04, and 6.05 files all have their main data section (#2)
  begin at offset 128. The short structure is because 6.05 files
  deviate after the 8th byte. So, grab the first 8 bytes to ensure it's
  valid, get the file version, then continue on with the correct
  structures.
  """

  _INITIAL_FILE_OFFSET = None

  NAME = 'java_idx'
  DESCRIPTION = 'Parser for Java WebStart Cache IDX files.'

  IDX_SHORT_STRUCT = construct.Struct(
      'magic',
      construct.UBInt8('busy'),
      construct.UBInt8('incomplete'),
      construct.UBInt32('idx_version'))

  IDX_602_STRUCT = construct.Struct(
      'IDX_602_Full',
      construct.UBInt16('null_space'),
      construct.UBInt8('shortcut'),
      construct.UBInt32('content_length'),
      construct.UBInt64('last_modified_date'),
      construct.UBInt64('expiration_date'),
      construct.PascalString(
          'version_string', length_field=construct.UBInt16('length')),
      construct.PascalString(
          'url', length_field=construct.UBInt16('length')),
      construct.PascalString(
          'namespace', length_field=construct.UBInt16('length')),
      construct.UBInt32('FieldCount'))

  IDX_605_SECTION_ONE_STRUCT = construct.Struct(
      'IDX_605_Section1',
      construct.UBInt8('shortcut'),
      construct.UBInt32('content_length'),
      construct.UBInt64('last_modified_date'),
      construct.UBInt64('expiration_date'),
      construct.UBInt64('validation_date'),
      construct.UBInt8('signed'),
      construct.UBInt32('sec2len'),
      construct.UBInt32('sec3len'),
      construct.UBInt32('sec4len'))

  IDX_605_SECTION_TWO_STRUCT = construct.Struct(
      'IDX_605_Section2',
      construct.PascalString(
          'version', length_field=construct.UBInt16('length')),
      construct.PascalString(
          'url', length_field=construct.UBInt16('length')),
      construct.PascalString(
          'namespec', length_field=construct.UBInt16('length')),
      construct.PascalString(
          'ip_address', length_field=construct.UBInt16('length')),
      construct.UBInt32('FieldCount'))

  # Java uses Pascal-style strings, but with a 2-byte length field.
  JAVA_READUTF_STRING = construct.Struct(
      'Java.ReadUTF',
      construct.PascalString(
          'string', length_field=construct.UBInt16('length')))

  def ParseFileObject(self, parser_mediator, file_object, **kwargs):
    """Parses a Java WebStart Cache IDX file-like object.

    Args:
      parser_mediator: A parser mediator object (instance of ParserMediator).
      file_object: A file-like object.

    Raises:
      UnableToParseFile: when the file cannot be parsed.
    """
    file_object.seek(0, os.SEEK_SET)
    try:
      magic = self.IDX_SHORT_STRUCT.parse_stream(file_object)
    except (IOError, construct.FieldError) as exception:
      raise errors.UnableToParseFile(
          'Unable to parse Java IDX file with error: {0!s}.'.format(exception))

    # Fields magic.busy and magic.incomplete are normally 0x00. They
    # are set to 0x01 if the file is currently being downloaded. Logic
    # checks for > 1 to avoid a race condition and still reject any
    # file with other data.
    # Field magic.idx_version is the file version, of which only
    # certain versions are supported.
    if magic.busy > 1 or magic.incomplete > 1:
      raise errors.UnableToParseFile('Not a valid Java IDX file')

    if not magic.idx_version in [602, 603, 604, 605]:
      raise errors.UnableToParseFile('Not a valid Java IDX file')

    # Obtain the relevant values from the file. The last modified date
    # denotes when the file was last modified on the HOST. For example,
    # when the file was uploaded to a web server.
    if magic.idx_version == 602:
      section_one = self.IDX_602_STRUCT.parse_stream(file_object)
      last_modified_date = section_one.last_modified_date
      url = section_one.url
      ip_address = 'Unknown'
      http_header_count = section_one.FieldCount
    elif magic.idx_version in [603, 604, 605]:

      # IDX 6.03 and 6.04 have two unused bytes before the structure.
      if magic.idx_version in [603, 604]:
        file_object.read(2)

      # IDX 6.03, 6.04, and 6.05 files use the same structures for the
      # remaining data.
      section_one = self.IDX_605_SECTION_ONE_STRUCT.parse_stream(file_object)
      last_modified_date = section_one.last_modified_date
      if file_object.get_size() > 128:
        file_object.seek(128, os.SEEK_SET)  # Static offset for section 2.
        section_two = self.IDX_605_SECTION_TWO_STRUCT.parse_stream(file_object)
        url = section_two.url
        ip_address = section_two.ip_address
        http_header_count = section_two.FieldCount
      else:
        url = 'Unknown'
        ip_address = 'Unknown'
        http_header_count = 0

    # File offset is now just prior to HTTP headers. Make sure there
    # are headers, and then parse them to retrieve the download date.
    download_date = None
    for field in range(0, http_header_count):
      field = self.JAVA_READUTF_STRING.parse_stream(file_object)
      value = self.JAVA_READUTF_STRING.parse_stream(file_object)
      if field.string == 'date':
        # Time string "should" be in UTC or have an associated time zone
        # information in the string itself. If that is not the case then
        # there is no reliable method for plaso to determine the proper
        # timezone, so the assumption is that it is UTC.
        try:
          download_date = timelib.Timestamp.FromTimeString(
              value.string, gmt_as_timezone=False)
        except errors.TimestampError:
          download_date = None
          parser_mediator.ProduceExtractionError(
              'Unable to parse time value: {0:s}'.format(value.string))

    if not url or not ip_address:
      raise errors.UnableToParseFile(
          'Unexpected Error: URL or IP address not found in file.')

    event_data = JavaIDXEventData()
    event_data.idx_version = magic.idx_version
    event_data.ip_address = ip_address
    event_data.url = url

    date_time = dfdatetime_java_time.JavaTime(timestamp=last_modified_date)
    # TODO: Move the timestamp description into eventdata.
    event = time_events.DateTimeValuesEvent(date_time, 'File Hosted Date')
    parser_mediator.ProduceEventWithEventData(event, event_data)

    if section_one:
      expiration_date = section_one.get('expiration_date', None)
      if expiration_date:
        date_time = dfdatetime_java_time.JavaTime(
            timestamp=expiration_date)
        event = time_events.DateTimeValuesEvent(
            date_time, definitions.TIME_DESCRIPTION_EXPIRATION)
        parser_mediator.ProduceEventWithEventData(event, event_data)

    if download_date:
      event = time_events.TimestampEvent(
          download_date, definitions.TIME_DESCRIPTION_FILE_DOWNLOADED)
      parser_mediator.ProduceEventWithEventData(event, event_data)


manager.ParsersManager.RegisterParser(JavaIDXParser)
