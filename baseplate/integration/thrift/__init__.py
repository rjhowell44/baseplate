"""Thrift integration for Baseplate.

This module provides an implementation of :py:class:`TProcessorEventHandler`
which integrates Baseplate's facilities into the Thrift request lifecycle.

An abbreviated example of it in use::

    def make_processor(app_config):
        baseplate = Baseplate()

        handler = MyHandler()
        processor = my_thrift.MyService.ContextProcessor(handler)

        event_handler = BaseplateProcessorEventHandler(logger, baseplate)
        processor.setEventHandler(event_handler)

        return processor

"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import sys

from thrift.Thrift import TProcessorEventHandler

from ...core import TraceInfo


TRACE_HEADER_NAMES = {
    "trace_id": (b"Trace", b"B3-TraceId"),
    "span_id": (b"Span", b"B3-SpanId"),
    "parent_span_id": (b"Parent", b"B3-ParentSpanId"),
    "sampled": (b"Sampled", b"B3-Sampled"),
    "flags": (b"Flags", b"B3-Flags"),
}


class RequestContext(object):
    pass


# TODO: exceptions in the event handler cause the connection to be abruptly
# closed with no diagnostics sent to the client. that should be more obvious.
class BaseplateProcessorEventHandler(TProcessorEventHandler):
    """Processor event handler for Baseplate.

    :param logging.Logger logger: The logger to use for error and debug logging.
    :param baseplate.core.Baseplate baseplate: The baseplate instance for your
        application.
    :param baseplate.core.EdgeRequestContextFactory edge_context_factory: A
        configured factory for handling edge request context.

    """
    def __init__(self, logger, baseplate, edge_context_factory=None):
        self.logger = logger
        self.baseplate = baseplate
        self.edge_context_factory = edge_context_factory

    def getHandlerContext(self, fn_name, server_context):
        context = RequestContext()

        trace_info = None
        headers = server_context.iprot.trans.get_headers()
        try:
            trace_info = self._get_trace_info(headers)
            edge_payload = headers.get(b"Edge-Request", None)
            if self.edge_context_factory:
                edge_context = self.edge_context_factory.from_upstream(
                    edge_payload)
                edge_context.attach_context(context)
            else:
                # just attach the raw context so it gets passed on
                # downstream even if we don't know how to handle it.
                context.raw_request_context = edge_payload
        except (KeyError, ValueError):
            pass

        trace = self.baseplate.make_server_span(
            context,
            name=fn_name,
            trace_info=trace_info,
        )

        try:
            peer_address, peer_port = server_context.getPeerName()
        except AttributeError:
            # the client transport is not a socket
            pass
        else:
            trace.set_tag("peer.ipv4", peer_address)
            trace.set_tag("peer.port", peer_port)

        context.headers = headers
        context.trace = trace
        return context

    def postRead(self, handler_context, fn_name, args):
        self.logger.debug("Handling: %r", fn_name)
        handler_context.trace.start()

    def handlerDone(self, handler_context, fn_name, result):
        if not getattr(handler_context.trace, "is_finished", False):
            # for unexpected exceptions, we call trace.finish() in handlerError
            handler_context.trace.finish()

    def handlerError(self, handler_context, fn_name, exception):
        handler_context.trace.finish(exc_info=sys.exc_info())
        handler_context.trace.is_finished = True
        self.logger.exception("Unexpected exception in %r.", fn_name)

    def _get_trace_info(self, headers):
        extracted_values = TraceInfo.extract_upstream_header_values(TRACE_HEADER_NAMES, headers)
        flags = extracted_values.get("flags", None)
        return TraceInfo.from_upstream(
            int(extracted_values["trace_id"]),
            int(extracted_values["parent_span_id"]),
            int(extracted_values["span_id"]),
            True if extracted_values["sampled"].decode("utf-8") == "1" else False,
            int(flags) if flags is not None else None,
        )
