"""Owned HTTP fixtures only; engine/model identity and capability are not inferred."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import sys
import threading
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'hosted/experimental'))
from cellivane_bridge import adapters
from cellivane_bridge.contracts import EngineConfig


def ollama(**changes):
    return {'model': 'small:fixture', 'created_at': '2026-10-09T00:00:00Z',
        'message': {'role': 'assistant', 'content': 'fixture answer'}, 'done': True,
        'done_reason': 'stop', 'prompt_eval_count': 3, 'eval_count': 2, **changes}


def openai(**changes):
    return {'id': 'fixture-result', 'object': 'chat.completion', 'created': 10,
        'model': 'small:fixture', 'choices': [{'index': 0,
        'message': {'role': 'assistant', 'content': 'fixture answer'}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}, **changes}


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode('utf-8')


@contextmanager
def fixture(routes, *, gate=None):
    calls = []
    received = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def handle_call(self):
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(min(length, 64 * 1024))
            calls.append({'method': self.command, 'path': self.path, 'body': body,
                          'client': self.client_address})
            received.set()
            if gate is not None:
                gate.wait(3)
            entry = routes.get(self.path, (404, b'{}', {}))
            if callable(entry):
                entry = entry(body)
            status, raw, headers = entry
            try:
                self.send_response(status)
                self.send_header('Content-Type', headers.get('Content-Type', 'application/json'))
                self.send_header('Content-Length', headers.get('Content-Length', str(len(raw))))
                self.send_header('Connection', 'close')
                for key, value in headers.items():
                    if key not in ('Content-Type', 'Content-Length'):
                        self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)
            except OSError:
                pass
        do_GET = do_POST = handle_call
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        yield server.server_port, calls, received
    finally:
        if gate is not None:
            gate.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


class BridgeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.messages = [{'role': 'system', 'content': 'Be brief.'}, {'role': 'user', 'content': 'hi'}]

    def engine(self, port=1, kind='ollama', **options):
        return EngineConfig('local-reference' if kind == 'openai' else 'local-candidate', kind,
            'http://127.0.0.1:' + str(port), 'small:fixture', **options)

    def call(self, response, kind='ollama', **options):
        path = '/api/chat' if kind == 'ollama' else '/v1/chat/completions'
        with fixture({path: (200, response if type(response) is bytes else encoded(response), {})}) as (port, calls, _):
            result = adapters.request(self.engine(port, kind, **options), self.messages, threading.Event())
            self.assertEqual(len(calls), 1)
            return result, calls[0]

    def test_ollama_serialization_answer_hash_and_native_counts(self):
        raw = encoded(ollama())
        result, call = self.call(raw)
        body = json.loads(call['body'])
        self.assertEqual(body, {'model': 'small:fixture', 'messages': self.messages,
                               'stream': False, 'think': False, 'options': {'num_predict': 128}})
        self.assertEqual(result.outcome, 'ANSWERED')
        self.assertEqual(result.text, 'fixture answer')
        self.assertEqual(result.request_sha256, hashlib.sha256(call['body']).hexdigest())
        self.assertEqual(result.response_sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(result.request_body.encode('utf-8'), call['body'])
        self.assertEqual(result.response_body.encode('utf-8'), raw)
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.usage, {'prompt_eval_count': 3, 'eval_count': 2})
        self.assertEqual(result.raw_response, ollama())

    def test_openai_base_v1_does_not_duplicate_prefix_and_reference_is_label_only(self):
        with fixture({'/v1/chat/completions': (200, encoded(openai()), {})}) as (port, calls, _):
            engine = replace(self.engine(port, 'openai', role='reference'),
                             name='Strata-config-label', base_url='http://127.0.0.1:' + str(port) + '/v1')
            result = adapters.request(engine, self.messages, threading.Event())
        self.assertEqual(result.outcome, 'ANSWERED')
        self.assertEqual(result.name, 'Strata-config-label')
        self.assertEqual(result.kind, 'openai')
        self.assertEqual(json.loads(calls[0]['body']), {'model': engine.model, 'messages': self.messages,
            'stream': False, 'max_tokens': 128, 'temperature': 0})
        self.assertEqual(result.usage, {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5})

    def test_cancel_before_dispatch_is_not_started_with_zero_connections(self):
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(adapters.http.client, 'HTTPConnection') as connection:
            result = adapters.request(self.engine(), self.messages, cancel)
        connection.assert_not_called()
        self.assertEqual(result.outcome, 'NOT_STARTED')
        self.assertIsNone(result.response_sha256)
        self.assertIsNone(result.raw_response)
        self.assertIsNone(result.response_body)
        self.assertIsNone(result.http_status)

    def test_complete_valid_late_response_is_answered_for_parent_output_fence(self):
        cancel = threading.Event()
        def finish(*_args):
            cancel.set()
            return encoded(ollama()), 200
        with mock.patch.object(adapters, '_exchange', side_effect=finish) as exchange:
            result = adapters.request(self.engine(), self.messages, cancel)
        self.assertEqual(result.outcome, 'ANSWERED')
        self.assertTrue(cancel.is_set())
        self.assertEqual(exchange.call_count, 1)

    def test_cancel_after_dispatch_stops_only_owned_connection_and_is_unknown(self):
        gate = threading.Event()
        with fixture({'/api/chat': (200, encoded(ollama()), {})}, gate=gate) as (port, calls, received):
            cancel, results = threading.Event(), []
            thread = threading.Thread(target=lambda: results.append(
                adapters.request(self.engine(port), self.messages, cancel)))
            thread.start()
            self.assertTrue(received.wait(2))
            cancel.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results[0].outcome, 'UNKNOWN')
            self.assertEqual(len(calls), 1)
            gate.set()
            result = adapters.request(self.engine(port), self.messages, threading.Event())
            self.assertEqual(result.outcome, 'ANSWERED')  # Fixture engine was not stopped.

    def test_wall_clock_timeout_is_bounded_and_never_retries(self):
        gate = threading.Event()
        with fixture({'/api/chat': (200, encoded(ollama()), {})}, gate=gate) as (port, calls, _):
            started = time.monotonic()
            result = adapters.request(self.engine(port, timeout_s=.1), self.messages, threading.Event())
            self.assertEqual(result.outcome, 'UNKNOWN')
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(len(calls), 1)

    def test_server_delay_longer_than_read_poll_keeps_the_same_request(self):
        gate = threading.Event()
        with fixture({'/api/chat': (200, encoded(ollama()), {})}, gate=gate) as (port, calls, received):
            results = []
            thread = threading.Thread(target=lambda: results.append(
                adapters.request(self.engine(port, timeout_s=1), self.messages, threading.Event())))
            thread.start()
            try:
                self.assertTrue(received.wait(1))
                time.sleep(.15)
                gate.set()
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertEqual(results[0].outcome, 'ANSWERED')
                self.assertEqual(len(calls), 1)
            finally:
                gate.set()
                thread.join(2)

    def test_redirect_and_server_error_preserve_unknown_without_follow_or_retry(self):
        with fixture({'/api/chat': (200, encoded(ollama()), {})}) as (target, target_calls, _):
            for status in (302, 500):
                headers = {'Location': 'http://127.0.0.1:' + str(target) + '/api/chat'}
                with fixture({'/api/chat': (status, b'{"error":"fixture"}', headers)}) as (port, calls, _):
                    result = adapters.request(self.engine(port), self.messages, threading.Event())
                    self.assertEqual(result.outcome, 'UNKNOWN')
                    self.assertEqual(result.error, 'http-status')
                    self.assertEqual(result.raw_response, {'error': 'fixture'})
                    self.assertEqual(result.response_body, '{"error":"fixture"}')
                    self.assertEqual(result.http_status, status)
                    self.assertEqual(len(calls), 1)
            self.assertFalse(target_calls)

    def test_connection_failure_is_unknown_not_not_started(self):
        with mock.patch.object(adapters.http.client.HTTPConnection, 'connect', side_effect=OSError('fixture')):
            result = adapters.request(self.engine(), self.messages, threading.Event())
        self.assertEqual(result.outcome, 'UNKNOWN')
        self.assertEqual(result.error, 'http-transport')

    def test_url_rejection_and_mutated_config_fail_before_io(self):
        urls = ['https://127.0.0.1:1', 'http://localhost:1', 'http://127.0.0.2:1',
            'http://user:pass@127.0.0.1:1', 'http://127.0.0.1:1?q=1', 'http://127.0.0.1:1#x',
            'http://127.0.0.1:1/api/chat', 'http://127.0.0.1', 'http://127.0.0.1:1/v1/']
        with mock.patch.object(adapters.http.client, 'HTTPConnection') as connection:
            for url in urls:
                engine = self.engine()
                engine.__dict__['base_url'] = url
                with self.subTest(url=url), self.assertRaises(ValueError):
                    adapters.request(engine, self.messages, threading.Event())
            for name, value in (('timeout_s', True), ('max_tokens', True), ('timeout_s', float('nan')),
                                ('kind', 'strata'), ('role', 'admin')):
                engine = self.engine()
                engine.__dict__[name] = value
                with self.subTest(name=name), self.assertRaises(ValueError):
                    adapters.request(engine, self.messages, threading.Event())
            connection.assert_not_called()

    def test_invalid_messages_and_byte_limit_fail_before_io(self):
        with mock.patch.object(adapters.http.client, 'HTTPConnection') as connection:
            for messages in ([], 'raw', [{'role': 'tool', 'content': 'tool'}],
                    [{'role': 'user', 'content': ' '}], [{'role': 'user', 'content': 'x\x00'}],
                    [{'role': 'user', 'content': 'x', 'tools': []}],
                    [{'role': 'user', 'content': '한' * 6000}],
                    [{'role': 'user', 'content': 'x'}] * 33,
                    [{'role': 'assistant', 'content': 'missing user'}]):
                with self.subTest(messages=str(messages)[:50]), self.assertRaises(ValueError):
                    adapters.request(self.engine(), messages, threading.Event())
            connection.assert_not_called()

    def test_ollama_invalid_completion_preserves_raw_but_is_unknown(self):
        for changes in ({'model': 'other'}, {'done': 1}, {'done': False}, {'done_reason': 'length'},
                {'message': {'role': 'user', 'content': 'hi'}}, {'eval_count': True},
                {'eval_count': -1}, {'thinking': 'hidden'}, {'usage': {}},
                {'message': {'role': 'assistant', 'content': '', 'tool_calls': []}}):
            with self.subTest(changes=changes):
                result, _ = self.call(ollama(**changes))
                self.assertEqual(result.outcome, 'UNKNOWN')
                self.assertIsNone(result.text)
                self.assertEqual(result.raw_response, ollama(**changes))
        result, _ = self.call(ollama(done_reason='length'))
        self.assertEqual(result.error, 'response-finish-length')

    def test_openai_invalid_finish_usage_choice_and_model_are_unknown(self):
        original = openai()
        choice = original['choices'][0]
        alternatives = [openai(model='other'), openai(created=True),
            openai(choices=[{**choice, 'index': True}]), openai(choices=[{**choice, 'finish_reason': 'length'}]),
            openai(choices=[{**choice, 'finish_reason': 'tool_calls'}]), openai(choices=[]),
            openai(usage={'prompt_tokens': True}), openai(usage={'total_tokens': -1}),
            openai(usage={'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 6}),
            openai(choices=[{**choice, 'message': {'role': 'assistant', 'content': 'hi', 'tool_calls': []}}])]
        for response in alternatives:
            with self.subTest(response=response):
                result, _ = self.call(response, 'openai')
                self.assertEqual(result.outcome, 'UNKNOWN')

    def test_duplicate_nonfinite_overflow_depth_and_nonobject_json_rejected(self):
        for raw in (b'{"model":"a","model":"b"}', b'{"x":NaN}', b'{"x":1e999}',
                    b'[]', b'\xff', b'{"x":' + b'[' * 20 + b'0' + b']' * 20 + b'}'):
            with self.subTest(raw=raw[:40]):
                result, _ = self.call(raw)
                self.assertEqual(result.outcome, 'UNKNOWN')
                self.assertIsNone(result.text)
                self.assertEqual(result.response_sha256, hashlib.sha256(raw).hexdigest())
                self.assertEqual(result.http_status, 200)
                self.assertEqual(result.response_body, None if raw == b'\xff' else raw.decode('utf-8'))

    def test_oversized_truncated_and_wrong_content_type_responses_are_unknown(self):
        entries = [(b'x' * (adapters.MAX_RESPONSE + 1), {}),
            (encoded(ollama()), {'Content-Length': str(len(encoded(ollama())) + 10)}),
            (encoded(ollama()), {'Content-Type': 'text/plain'}),
            (encoded(ollama()), {'Content-Encoding': 'gzip'})]
        for raw, headers in entries:
            with self.subTest(headers=headers), fixture({'/api/chat': (200, raw, headers)}) as (port, calls, _):
                result = adapters.request(self.engine(port), self.messages, threading.Event())
                self.assertEqual(result.outcome, 'UNKNOWN')
                self.assertEqual(len(calls), 1)

    def test_proxy_environment_is_not_used(self):
        with fixture({'/api/chat': (200, encoded(ollama()), {})}) as (target, calls, _):
            with fixture({}) as (proxy, proxy_calls, _):
                with mock.patch.dict(os.environ, {'HTTP_PROXY': 'http://127.0.0.1:' + str(proxy),
                        'ALL_PROXY': 'http://127.0.0.1:' + str(proxy), 'NO_PROXY': ''}):
                    result = adapters.request(self.engine(target), self.messages, threading.Event())
                self.assertEqual(result.outcome, 'ANSWERED')
                self.assertEqual(len(calls), 1)
                self.assertFalse(proxy_calls)

    def test_parallel_calls_use_distinct_owned_connections(self):
        barrier = threading.Barrier(2)
        def answer(_body):
            barrier.wait(2)
            return 200, encoded(ollama()), {}
        with fixture({'/api/chat': answer}) as (port, calls, _):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: adapters.request(
                    self.engine(port), self.messages, threading.Event()), range(2)))
            self.assertEqual([result.outcome for result in results], ['ANSWERED', 'ANSWERED'])
            self.assertEqual(len({call['client'] for call in calls}), 2)

    def test_ollama_probe_records_actual_version_digest_and_exact_model(self):
        routes = {'/api/version': (200, encoded({'version': 'fixture-version'}), {}),
            '/api/tags': (200, encoded({'models': [{'name': 'small:fixture', 'model': 'small:fixture',
                'digest': 'a' * 64, 'size': 10}]}), {})}
        with fixture(routes) as (port, calls, _):
            report = adapters.probe(self.engine(port))
        self.assertEqual(report['outcome'], 'CONFIRMED')
        self.assertTrue(report['model_present'])
        self.assertEqual(report['server_version'], 'fixture-version')
        self.assertEqual(report['model_digest'], 'a' * 64)
        self.assertEqual([call['path'] for call in calls], ['/api/version', '/api/tags'])
        self.assertTrue(all(call['method'] == 'GET' for call in calls))
        self.assertNotIn('capabilities', report)
        for response in report['responses']:
            self.assertEqual(hashlib.sha256(response['response_body'].encode('utf-8')).hexdigest(),
                             response['response_sha256'])
            self.assertEqual(json.loads(response['response_body']), response['raw_response'])

    def test_openai_probe_does_not_invent_strata_version_or_model_digest(self):
        routes = {'/health': (200, b'{"status":"ok"}', {}), '/v1/models': (200,
            encoded({'object': 'list', 'data': [{'id': 'small:fixture', 'object': 'model', 'created': 1}]}), {})}
        with fixture(routes) as (port, calls, _):
            report = adapters.probe(replace(self.engine(port, 'openai'), name='Strata'))
        self.assertEqual(report['outcome'], 'CONFIRMED')
        self.assertEqual(report['name'], 'Strata')
        self.assertIsNone(report['server_version'])
        self.assertIsNone(report['model_digest'])
        self.assertEqual([call['path'] for call in calls], ['/health', '/v1/models'])

    def test_probe_missing_ambiguous_model_bool_numbers_and_bad_digest_fail_closed(self):
        for models in ([], [{'name': 'other', 'digest': 'a' * 64}],
                [{'name': 'small:fixture', 'digest': 'a' * 64}] * 2,
                [{'name': 'small:fixture', 'digest': 'bad'}],
                [{'name': 'small:fixture', 'digest': 'a' * 64, 'size': True}]):
            routes = {'/api/version': (200, b'{"version":"fixture"}', {}),
                      '/api/tags': (200, encoded({'models': models}), {})}
            with self.subTest(models=models), fixture(routes) as (port, calls, _):
                report = adapters.probe(self.engine(port))
                self.assertEqual(report['outcome'], 'UNKNOWN')
                self.assertIsNotNone(report['error'])
                self.assertEqual(len(calls), 2)

    def test_probe_bad_json_status_redirect_and_bound_never_retry(self):
        for status, raw in ((200, b'{"version":"a","version":"b"}'),
                            (302, b'{}'), (200, b'x' * (adapters.MAX_RESPONSE + 1))):
            with self.subTest(status=status), fixture({'/api/version': (status, raw, {})}) as (port, calls, _):
                report = adapters.probe(self.engine(port))
                self.assertEqual(report['outcome'], 'UNKNOWN')
                self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
