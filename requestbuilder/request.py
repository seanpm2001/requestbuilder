# Copyright (c) 2012-2013, Eucalyptus Systems, Inc.
#
# Permission to use, copy, modify, and/or distribute this software for
# any purpose with or without fee is hereby granted, provided that the
# above copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT
# OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

from __future__ import absolute_import

from functools import partial
import logging
import platform
import sys
import textwrap

from . import __version__, EMPTY
from .command import BaseCommand
from .exceptions import ClientError, ServerError
from .service import BaseService
from .util import aggregate_subclass_fields, get_default_user_agent
from .xmlparse import parse_listdelimited_aws_xml

class BaseRequest(BaseCommand):
    '''
    The basis for a command line tool that represents a request.  To invoke
    this as a command line tool, call the do_cli() method on an instance of the
    class; arguments will be parsed from the command line.  To invoke this in
    another context, pass keyword args to __init__() with names that match
    those stored by the argument parser and then call main().

    Important methods in this class include:
     - do_cli:       command line entry point
     - main:         pre/post-request processing and request sending
     - send:         actually send a request to the server and return a
                     response (called by the main() method)
     - print_result: format data from the main method and print it to stdout

    To be useful a tool should inherit from this class and implement the main()
    and print_result() methods.  The do_cli() method functions as the entry
    point for the command line, populating self.args from the command line and
    then calling main() and print_result() in sequence.  Other tools may
    instead supply arguments via __init__() and then call main() alone.

    Important members of this class include:
     - SERVICE_CLASS: a class corresponding to the web service in use
     - NAME:          a string containing the Action query parameter.  This
                      defaults to the class's name.
     - DESCRIPTION:   a string describing the tool.  This becomes part of the
                      command line help string.
     - ARGS:          a list of Arg and/or MutuallyExclusiveArgGroup objects
                      are used to generate command line arguments.  Inheriting
                      classes needing to add command line arguments should
                      contain their own Args lists, which are *prepended* to
                      those of their parent classes.
     - FILTERS:       a list of Filter objects that are used to generate filter
                      options at the command line.  Inheriting classes needing
                      to add filters should contain their own FILTERS lists,
                      which are *prepended* to those of their parent classes.
    '''

    SERVICE_CLASS = BaseService
    NAME          = None
    METHOD        = 'GET'

    FILTERS = []
    LIST_MARKERS = []


    def __init__(self, service=None, **kwargs):
        self.service = service
        # Parts of the HTTP request to be sent to the server.
        self.method    = self.METHOD
        self.path      = None
        self.headers   = {}
        self.params    = {}
        self.body      = None

        # HTTP response obtained from the server
        self.response = None

        self.__user_agent = None

        BaseCommand.__init__(self, **kwargs)

    def _post_init(self):
        if self.service is None:
            self.service = self.SERVICE_CLASS(self.config, self.log)
        BaseCommand._post_init(self)

    @property
    def default_route(self):
        return self.params

    def collect_arg_objs(self):
        request_args = BaseCommand.collect_arg_objs(self)
        service_args = self.service.collect_arg_objs()
        # Note that the service is likely to include auth args as well.
        return request_args + service_args

    def preprocess_arg_objs(self, arg_objs):
        self.service.preprocess_arg_objs(arg_objs)

    def configure(self):
        self.service.configure()

    @property
    def name(self):
        '''
        The name of this action.  Used when choosing what to supply for the
        Action query parameter.
        '''
        return self.NAME or self.__class__.__name__

    @property
    def user_agent(self):
        '''
        Return a user-agent string for this program.
        '''
        if not self.__user_agent:
            self.__user_agent = get_default_user_agent()
        return self.__user_agent

    @property
    def status(self):
        if self.response is not None:
            return self.response.status
        else:
            return None

    def send(self):
        headers = dict(self.headers or {})
        headers.setdefault('User-Agent', self.user_agent)
        params  = self.prepare_params()
        try:
            self.response = self.service.send_request(method=self.method,
                    path=self.path, headers=headers, params=params,
                    data=self.body)
            return self.parse_response(self.response)
        except ServerError as err:
            self.response = err.response
            return self.handle_server_error(err)
        finally:
            # Empty the socket buffer so it can be reused
            try:
                if self.response is not None:
                    self.response.content
            except RuntimeError:
                # The content was already consumed
                pass

    def handle_server_error(self, err):
        self.log.debug('-- response content --\n',
                       extra={'append': True})
        self.log.debug(self.response.text, extra={'append': True})
        self.log.debug('-- end of response content --')
        self.log.info('result: failure')
        raise

    def prepare_params(self):
        return self.params or {}

    def parse_response(self, response):
        return response

    def log_and_parse_response(self, response, parse_func, **kwargs):
        # We do some extra handling here to log stuff as it comes in rather
        # than reading it all into memory at once.
        self.log.debug('-- response content --\n', extra={'append': True})
        # Using Response.iter_content gives us automatic decoding, but we then
        # have to make the generator look like a file so etree can use it.
        with _IteratorFileObjAdapter(self.response.iter_content(16384)) \
                as content_fileobj:
            logged_fileobj = _ReadLoggingFileWrapper(content_fileobj, self.log,
                                                     logging.DEBUG)
            parsed_response = parse_func(logged_fileobj, **kwargs)
        self.log.debug('-- end of response content --')
        return parsed_response

    def main(self):
        '''
        The main processing method for this type of request.  In this method,
        inheriting classes generally populate self.headers, self.params, and
        self.body with information gathered from self.args or elsewhere,
        call self.send, and return the response.  BaseRequest's default
        behavior is to simply return the result of a request with everything
        that routes to PARAMS.
        '''
        self.preprocess()
        response = self.send()
        self.postprocess(response)
        return response

    def preprocess(self):
        pass

    def postprocess(self, response):
        pass

    def handle_cli_exception(self, err):
        if isinstance(err, ServerError):
            print >> sys.stderr, 'error ', str(err)
            if self.debug:
                raise
            sys.exit(1)
        else:
            BaseCommand.handle_cli_exception(self, err)


class AWSQueryRequest(BaseRequest):
    API_VERSION = None

    def populate_parser(self, parser, arg_objs):
        BaseRequest.populate_parser(self, parser, arg_objs)
        if self.FILTERS:
            parser.add_argument('--filter', metavar='NAME=VALUE',
                    action='append', dest='filters',
                    help='restrict results to those that meet criteria',
                    type=partial(_parse_filter, filter_objs=self.FILTERS))
            parser.epilog = self.__build_filter_help()
            self._arg_routes['filters'] = None

    def process_cli_args(self):
        BaseRequest.process_cli_args(self)
        if 'filters' in self.args:
            self.args['Filter'] = _process_filters(self.args.pop('filters'))
            self._arg_routes['Filter'] = self.params

    def prepare_params(self):
        params = self.flatten_params(self.params)
        params['Action'] = self.name
        params['Version'] = self.API_VERSION or self.service.API_VERSION
        self.log.info('parameters: %s', params)
        return params

    def parse_response(self, response):
        # Parser for list-delimited responses like EC2's
        response_dict = self.log_and_parse_response(response,
                parse_listdelimited_aws_xml, list_markers=self.LIST_MARKERS)
        # Strip off the root element
        assert len(response_dict) == 1
        return response_dict[list(response_dict.keys())[0]]

    def flatten_params(self, args, prefix=None):
        '''
        Given a possibly-nested dict of args and an arg routing destination,
        transform each element in the dict that matches the corresponding
        arg routing table into a simple dict containing key-value pairs
        suitable for use as query parameters.  This implementation flattens
        dicts and lists into the format given by the EC2 query API, which uses
        dotted lists of dict keys and list indices to indicate nested
        structures.

        Keys with nonzero values that evaluate as false are ignored.  If a
        collection of keys is supplied with ignore then keys that do not
        appear in that collection are also ignored.

        Examples:
          in:  {'InstanceId': 'i-12345678', 'PublicIp': '1.2.3.4'}
          out: {'InstanceId': 'i-12345678', 'PublicIp': '1.2.3.4'}

          in:  {'RegionName': ['us-east-1', 'us-west-1']}
          out: {'RegionName.1': 'us-east-1',
                'RegionName.2': 'us-west-1'}

          in:  {'Filter': [{'Name':  'image-id',
                            'Value': ['ami-12345678']},
                           {'Name':  'instance-type',
                            'Value': ['m1.small', 't1.micro']}],
                'InstanceId': ['i-24680135']}
          out: {'Filter.1.Name':    'image-id',
                'Filter.1.Value.1': 'ami-12345678',
                'Filter.2.Name':    'instance-type',
                'Filter.2.Value.1': 'm1.small',
                'Filter.2.Value.2': 't1.micro',
                'InstanceId.1':     'i-24680135'}
        '''
        flattened = {}
        if args is None:
            return {}
        elif isinstance(args, dict):
            for (key, val) in args.iteritems():
                # Prefix.Key1, Prefix.Key2, ...
                    if prefix:
                        prefixed_key = prefix + '.' + str(key)
                    else:
                        prefixed_key = str(key)

                    if isinstance(val, dict) or isinstance(val, list):
                        flattened.update(self.flatten_params(val, prefixed_key))
                    elif isinstance(val, file):
                        flattened[prefixed_key] = val.read()
                    elif val or val is 0:
                        flattened[prefixed_key] = str(val)
                    elif val is EMPTY:
                        flattened[prefixed_key] = ''
        elif isinstance(args, list):
            for (i_item, item) in enumerate(args, 1):
                # Prefix.1, Prefix.2, ...
                if prefix:
                    prefixed_key = prefix + '.' + str(i_item)
                else:
                    prefixed_key = str(i_item)

                if isinstance(item, dict) or isinstance(item, list):
                    flattened.update(self.flatten_params(item, prefixed_key))
                elif isinstance(item, file):
                    flattened[prefixed_key] = item.read()
                elif item or item == 0:
                    flattened[prefixed_key] = str(item)
                elif val is EMPTY:
                    flattened[prefixed_key] = ''
        else:
            raise TypeError('non-flattenable type: ' + args.__class__.__name__)
        return flattened

    def __build_filter_help(self, force=False):
        '''
        Return a pre-formatted help string for all of the filters defined in
        self.FILTERS.  The result is meant to be used as command line help
        output.
        '''
        # Does not have access to self.config
        if '-h' not in sys.argv and '--help' not in sys.argv and not force:
            # Performance optimization
            return ''

        # FIXME:  This code has a bug with triple-quoted strings that contain
        #         embedded indentation.  textwrap.dedent doesn't seem to help.
        #         Reproducer: 'whether the   volume will be deleted'
        max_len = 24
        col_len = max([len(filter_obj.name) for filter_obj in self.FILTERS
                       if len(filter_obj.name) < max_len]) - 1
        helplines = ['allowed filter names:']
        for filter_obj in self.FILTERS:
            if filter_obj.help:
                if len(filter_obj.name) <= col_len:
                    # filter-name    Description of the filter that
                    #                continues on the next line
                    right_space = ' ' * (max_len - len(filter_obj.name) - 2)
                    wrapper = textwrap.TextWrapper(fix_sentence_endings=True,
                        initial_indent=('  ' + filter_obj.name + right_space),
                        subsequent_indent=(' ' * max_len))
                else:
                    # really-long-filter-name
                    #                Description that begins on the next line
                    helplines.append('  ' + filter_obj.name)
                    wrapper = textwrap.TextWrapper(fix_sentence_endings=True,
                            initial_indent=(   ' ' * max_len),
                            subsequent_indent=(' ' * max_len))
                helplines.extend(wrapper.wrap(filter_obj.help))
            else:
                helplines.append('  ' + filter_obj.name)
        return '\n'.join(helplines)


def _parse_filter(filter_str, filter_objs=None):
    '''
    Given a "key=value" string given as a command line parameter, return a pair
    with the matching filter's dest member and the given value after converting
    it to the type expected by the filter.  If this is impossible, an
    ArgumentTypeError will result instead.
    '''
    # Find the appropriate filter object
    filter_objs = [obj for obj in (filter_objs or [])
                   if obj.matches_argval(filter_str)]
    if not filter_objs:
        msg = '"{0}" matches no available filters'.format(filter_str)
        raise argparse.ArgumentTypeError(msg)
    return filter_objs[0].convert(filter_str)


def _process_filters(cli_filters):
    '''
    Change filters from the [(key, value), ...] format given at the command
    line to [{'Name': key, 'Value': [value, ...]}, ...] format, which
    flattens to the form the server expects.
    '''
    filter_args = {}
    # Compile [(key, value), ...] pairs into {key: [value, ...], ...}
    for (key, val) in cli_filters or {}:
        filter_args.setdefault(key, [])
        filter_args[key].append(val)
    # Build the flattenable [{'Name': key, 'Value': [value, ...]}, ...]
    filters = [{'Name': name, 'Value': values} for (name, values)
               in filter_args.iteritems()]
    return filters


class _IteratorFileObjAdapter(object):
    def __init__(self, source):
        self._source  = source
        self._buflist = []
        self._closed  = False
        self._len     = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @property
    def closed(self):
        return self._closed

    def close(self):
        if not self._closed:
            self.buflist = None
            self._closed = True

    def read(self, size=-1):
        if size is None or size < 0:
            for chunk in self._source:
                self._buflist.append(chunk)
            result = ''.join(self._buflist)
            self._buflist = []
            self._len     = 0
        else:
            while self._len < size:
                try:
                    chunk = next(self._source)
                    self._buflist.append(chunk)
                    self._len += len(chunk)
                except StopIteration:
                    break
            result    = ''.join(self._buflist)
            extra_len = len(result) - size
            self._buflist = []
            self._len     = 0
            if extra_len > 0:
                self._buflist = [result[-extra_len:]]
                self._len     = extra_len
                result = result[:-extra_len]
        return result


class _ReadLoggingFileWrapper(object):
    def __init__(self, fileobj, logger, level):
        self.fileobj = fileobj
        self.logger  = logger
        self.level   = level

    def read(self, size=-1):
        chunk = self.fileobj.read(size)
        self.logger.log(self.level, chunk, extra={'append': True})
        return chunk
