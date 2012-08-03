__license__ = """
Copyright 2012 DISQUS

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import logging
import random
import string
import time

from samsa.client import Client, Message, OFFSET_EARLIEST, OFFSET_LATEST
from samsa.exceptions import InvalidFetchSize, WrongPartition
from samsa.test.integration import KafkaIntegrationTestCase


logger = logging.getLogger(__name__)

# class ClientTestCase(unittest2.TestCase):
#     def test_produce(self):
#         raise NotImplementedError
#
#     def test_multiproduce(self):
#         raise NotImplementedError
#
#     def test_fetch(self):
#         raise NotImplementedError
#
#     def test_multifetch(self):
#         raise NotImplementedError
#
#     def test_offsets(self):
#         raise NotImplementedError


def filter_messages(stream):
    # TODO: this can deadlock, needs a timeout or something
    prefix = 'consumed: '
    while True:
        line = stream.readline().strip()
        if line.startswith(prefix):
            message = line[len(prefix):]
            yield message


class ClientIntegrationTestCase(KafkaIntegrationTestCase):
    def setUp(self):
        super(ClientIntegrationTestCase, self).setUp()
        self.kafka = Client(host='localhost', port=self.kafka_broker.port)

    def assertPassesWithMultipleAttempts(self, fn, attempts, timeout=1, backoff=None):
        if backoff is None:
            backoff = lambda attempt, timeout: timeout

        for attempt in xrange(1, attempts + 1):
            logger.debug('Starting attempt %s for %s...', attempt, fn)
            try:
                fn()
                logger.info('Passed attempt %s for %s', attempt, fn)
                break
            except AssertionError:
                if attempt < attempts:
                    wait = backoff(attempt, timeout)
                    logger.exception('Failed attempt %s for %s, waiting for %s seconds',
                        attempt, fn, wait)
                    time.sleep(wait)
                else:
                    raise

    def test_produce(self):
        topic = 'topic'
        message = 'hello world'
        consumer = self.consumer(topic)
        self.kafka.produce(topic, 0, (message,))

        consumed = next(filter_messages(consumer.process.stdout))
        self.assertEqual(consumed, message)

    def test_multiproduce(self):
        topics = ('topic-a', 'topic-b')

        def message_for_topic(topic):
            return 'hello to topic %s' % topic

        consumers = {}
        for topic in topics:
            consumer = self.consumer(topic)
            consumers[topic] = consumer

        batch = []
        for topic in topics:
            batch.append((topic, 0, (message_for_topic(topic),)))

        self.kafka.multiproduce(batch)

        for topic, consumer in consumers.items():
            consumed = next(filter_messages(consumer.process.stdout))
            self.assertEqual(consumed, message_for_topic(topic))

    def test_fetch(self):
        # TODO: test error conditions
        topic = 'topic'
        payload = 'hello world'
        size = 1024 * 300

        producer = self.producer(topic)
        producer.publish([payload])

        def ensure_valid_response():
            messages = list(self.kafka.fetch(topic, 0, 0, size))
            self.assertEqual(len(messages), 1)

            message = messages[0]
            self.assertIsInstance(message, Message)
            self.assertEqual(message.offset, 0)
            self.assertEqual(message.next_offset, len(message))
            self.assertEqual(message.payload, payload)
            self.assertEqual(message['compression'], 0)
            try:
                message.validate()
            except Exception, exc:
                self.fail('Message should pass checksum validation, instead got %s' % exc)

            self.offset = message.next_offset

        self.assertPassesWithMultipleAttempts(ensure_valid_response, 5)

        payloads = ['hello', 'world']
        producer.publish(payloads)

        def ensure_valid_response_again():
            messages = list(self.kafka.fetch(topic, 0, self.offset, size))
            self.assertEqual(len(messages), 2)
            self.assertTrue(all(isinstance(m, Message) for m in messages))
            self.assertEqual([m.payload for m in messages], payloads)
            self.assertEqual(messages[0].offset, self.offset)

        self.assertPassesWithMultipleAttempts(ensure_valid_response_again, 5)

    def test_fetch_sizing(self):
        topic = 'topic'
        partition = 0
        payload = ''.join(random.choice(string.ascii_letters) for _ in xrange(0, 300))

        producer = self.producer(topic)
        producer.publish([payload])

        def ensure_no_partial_messages():
            messages = list(self.kafka.fetch(topic, partition, 0, len(payload) // 2))
            self.assertEqual(len(messages), 0)

            messages = list(self.kafka.fetch(topic, partition, 0, 1024 * 300))
            self.assertEqual(len(messages), 1)
            message = messages[0]
            message.validate()
            self.assertEqual(message.payload, payload)

        self.assertPassesWithMultipleAttempts(ensure_no_partial_messages, 5)

    def test_fetch_wrong_partition(self):
        with self.assertRaises(WrongPartition):
            self.kafka.fetch('topic', 10, 0, 1024 * 300)

    def test_multifetch(self):
        # TODO: test error conditions
        topics = ('topic-a', 'topic-b')
        size = 1024 * 300

        def payload_for_topic(topic):
            return 'hello from topic %s' % topic

        producers = {}
        for topic in topics:
            producer = self.producer(topic)
            producer.publish([payload_for_topic(topic)])
            producers[topic] = producer

        def ensure_valid_response():
            batches = [(topic, 0, 0, size) for topic in topics]
            responses = self.kafka.multifetch(batches)

            self.next_offsets = {}
            num_responses = 0
            for topic, response in zip(topics, responses):
                messages = list(response)
                self.assertEqual(len(messages), 1)

                message = messages[0]
                self.assertIsInstance(message, Message)
                self.assertEqual(message.offset, 0)
                self.assertEqual(message.next_offset, len(message))
                self.next_offsets[topic] = message.next_offset
                self.assertEqual(message.payload, payload_for_topic(topic))
                self.assertEqual(message['compression'], 0)
                try:
                    message.validate()
                except Exception, exc:
                    self.fail('Message should pass checksum validation, instead got %s' % exc)

                num_responses += 1

            self.assertEqual(len(batches), num_responses)

        self.assertPassesWithMultipleAttempts(ensure_valid_response, 5)

        batches = []
        num_messages = 2
        for topic, producer in producers.items():
            payloads = [payload_for_topic(topic)] * num_messages
            producer.publish(payloads)
            batches.append((topic, 0, self.next_offsets[topic], size))

        def ensure_valid_response_again():
            responses = self.kafka.multifetch(batches)
            for topic, response in zip(topics, responses):
                messages = list(response)
                self.assertEqual(len(messages), num_messages)

                message = messages[0]
                self.assertEqual(message.offset, self.next_offsets[topic])
                self.assertEqual([m.payload for m in messages],
                    [payload_for_topic(topic)] * num_messages)

        self.assertPassesWithMultipleAttempts(ensure_valid_response_again, 5)

    def test_offsets(self):
        offsets = self.kafka.offsets('topic', 0, OFFSET_EARLIEST, 1)
        self.assertEqual(len(offsets), 1)
        self.assertEqual(offsets[0], 0)

        offsets = self.kafka.offsets('topic', 0, OFFSET_LATEST, 1)
        self.assertEqual(len(offsets), 1)
        self.assertEqual(offsets[0], 0)
