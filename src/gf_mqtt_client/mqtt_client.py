import asyncio
import json
import uuid
from typing import Optional, Dict, Any, Callable, Awaitable, List
import logging
from aiomqtt import Client
from .models import ResponseCode, Method
from .payload_handler import PayloadHandler
from .topic_manager import TopicManager
from .message_handler import (
    MessageHandlerBase,
    MessageHandlerProtocol,
    ResponseHandlerDefault,
)


class MQTTClient:
    def __init__(
        self,
        broker: str,
        port: int = 1883,
        timeout: int = 5,
        identifier: Optional[str] = None,
        subscriptions: Optional[list] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self.broker = broker
        self.port = port
        self.timeout = timeout
        self._username: Optional[str] = username
        self._password: Optional[str] = password
        self.identifier = identifier if identifier else f"mqtt_client_{uuid.uuid4()}"
        self._client: Optional[Client] = None
        self._client_task: Optional[asyncio.Task] = None
        self._pending_requests: Dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self._message_handlers: List[MessageHandlerBase] = []
        self._connected = asyncio.Event()
        self._topic_manager = TopicManager()
        self.subscriptions = subscriptions or []
        logging.info(f"Initialized MQTT client with identifier {self.identifier}")

    def set_credentials(self, username: str, password: str):
        self._username = username
        self._password = password
        logging.info(f"Credentials set for username {username} in client {self.identifier}")

    async def add_message_handler(self, handler: MessageHandlerProtocol):
        async with self._lock:
            if not isinstance(handler, MessageHandlerProtocol):
                raise ValueError("Handler must implement MessageHandlerProtocol")
            self._message_handlers.append(handler)
            logging.info(f"Added message handler {handler.__class__.__name__} to client {self.identifier}")

    async def remove_message_handler(self, handler: MessageHandlerBase):
        async with self._lock:
            if handler in self._message_handlers:
                self._message_handlers.remove(handler)
                logging.info(f"Removed message handler {handler.__class__.__name__} from client {self.identifier}")
            else:
                logging.warning(f"Handler {handler.__class__.__name__} not found in client {self.identifier}")

    def generate_request_id(self) -> str:
        # Generate a unique request ID using UUID
        logging.debug(f"Generating request ID for client {self.identifier}")
        return str(uuid.uuid4())
    
    async def connect(self):
        logging.info(f"Connecting to broker {self.broker}:{self.port} with client {self.identifier}")
        self._client = Client(
            hostname=self.broker,
            port=self.port,
            username=self._username,
            password=self._password,
            identifier=self.identifier,
        )
        await self._client.__aenter__()
        logging.info(f"Connected to broker with client {self.identifier}")

        self._client_task = asyncio.create_task(self._message_loop())
        self._connected.set()

        request_topic = self._topic_manager.build_request_topic(
            target_device_tag=self.identifier, subsystem="+", request_id="+"
        )
        self.subscriptions.append(request_topic)

        if self.subscriptions:
            for topic in self.subscriptions:
                await self._client.subscribe(topic)
                logging.info(f"Subscribed to topic {topic} with client {self.identifier}")

        await self.add_message_handler(ResponseHandlerDefault())
        logging.debug(f"Added default response handler for client {self.identifier}")

    async def disconnect(self):
        logging.info(f"Disconnecting MQTT client {self.identifier}")
        if self._client_task:
            self._client_task.cancel()
            try:
                await self._client_task
            except asyncio.CancelledError:
                logging.debug(f"Message loop task cancelled for client {self.identifier}")

        if self._client:
            await self._client.__aexit__(None, None, None)
            logging.info(f"Disconnected from broker with client {self.identifier}")

    async def _message_loop(self):
        async for message in self._client.messages:
            payload_str = message.payload.decode()
            try:
                payload = json.loads(payload_str)
                topic = message.topic
                logging.debug(f"Received message on topic {topic} with payload {payload} for client {self.identifier}")
                processed = False
                for handler in self._message_handlers:
                    if not handler.can_handle(self, topic, payload):
                        continue
                    response = await handler.handle(self, topic, payload)
                    if response is not None:
                        header = payload.get("header", {})
                        if "request_id" in header and "response_code" in header:
                            request_id = header["request_id"]
                            async with self._lock:
                                future = self._pending_requests.pop(request_id, None)
                            if future and not future.done():
                                future.set_result(payload)
                                logging.debug(f"Resolved request {request_id} with response {payload} for client {self.identifier}")
                        processed = True
                    if not handler.propagate:
                        break

                if not processed and any(
                    h.can_handle(self, message) for h in self._message_handlers
                ):
                    default_handler = MessageHandlerBase(
                        can_handle=lambda c, m: True,
                        process=self._default_handler,
                        priority=100,
                        propagate=True,
                    )
                    await default_handler.handle(self, topic, payload)

            except json.JSONDecodeError:
                logging.error(f"Invalid JSON received on topic {message.topic} for client {self.identifier}")
            except Exception as e:
                logging.exception(f"Error in message loop for client {self.identifier}: {e}")

    async def _default_handler(
        self, topic: str, payload: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        logging.info(f"Default handler processing unhandled message on topic {topic}: {payload} for client {self.identifier}")
        return payload

    async def publish(self, topic: str, payload: Dict[str, Any], qos: int = 0):
        if not self._client:
            logging.error(f"Cannot publish: Client {self.identifier} is not connected")
            raise RuntimeError("Client is not connected")

        payload_str = json.dumps(payload)
        await self._client.publish(topic, payload_str, qos=qos)
        logging.debug(f"Published message to topic {topic} with payload {payload} for client {self.identifier}")

    async def subscribe(self, topic: str):
        if not self._client:
            logging.error(f"Cannot subscribe: Client {self.identifier} is not connected")
            raise RuntimeError("Client is not connected")

        await self._client.subscribe(topic)
        logging.info(f"Subscribed to topic {topic} with client {self.identifier}")

    async def request(
        self, target_device_tag, subsystem, path: str
    ) -> Optional[Dict[str, Any]]:
        if not self._connected.is_set():
            logging.error(f"Cannot send request: Client {self.identifier} is not connected")
            raise RuntimeError("Client not connected")

        payload_handler = PayloadHandler()
        request_id = self.generate_request_id()
        logging.debug(f"Generated request ID {request_id} for client {self.identifier}")

        request_payload = payload_handler.create_request_payload(
            method=Method.GET, path=path, request_id=request_id
        )

        request_topic = self._topic_manager.build_request_topic(
            target_device_tag=target_device_tag,
            subsystem=subsystem,
            request_id=request_id,
        )
        response_topic = self._topic_manager.build_response_topic(
            request_topic=request_topic
        )

        future = asyncio.get_event_loop().create_future()
        async with self._lock:
            self._pending_requests[request_id] = future

        await self.subscribe(response_topic)
        await self.publish(request_topic, request_payload)
        logging.info(f"Sent request {request_id} to topic {request_topic} for client {self.identifier}")

        try:
            result = await asyncio.wait_for(future, timeout=self.timeout)
            logging.info(f"Received response for request {request_id} on topic {response_topic}: {result} for client {self.identifier}")
            return result
        except asyncio.TimeoutError:
            logging.warning(f"Request {request_id} timed out after {self.timeout} seconds for client {self.identifier}")
            async with self._lock:
                self._pending_requests.pop(request_id, None)
            return None
        finally:
            await self._client.unsubscribe(response_topic)
            logging.info(f"Unsubscribed from response topic {response_topic} for client {self.identifier}")
            async with self._lock:
                if request_id in self._pending_requests:
                    del self._pending_requests[request_id]
                    logging.debug(f"Removed pending request {request_id} from tracking for client {self.identifier}")


# Example usage of message handlers
if __name__ == "__main__":

    async def main():
        async def request_handler(payload: Dict[str, Any]) -> Dict[str, Any]:
            logging.info(f"Handling request: {payload}")
            payload_handler = PayloadHandler()
            return payload_handler.create_response_payload(
                response_code=ResponseCode.CONTENT,
                path=payload["header"]["path"],
                request_id=payload["header"]["request_id"],
                body={"data": [1, 2, 3]},
            )

        async def logging_handler(payload: Dict[str, Any]) -> Dict[str, Any]:
            logging.debug(f"Logging message: {payload}")
            return None

        async def response_handler(payload: Dict[str, Any]) -> Dict[str, Any]:
            if "response_code" in payload.get("header", {}):
                logging.info(f"Received response: {payload}")
            return None

        client = MQTTClient("localhost")
        client.add_message_handler(
            MessageHandlerBase(
                can_handle=lambda p: "method" in p.get("header", {}),
                process=request_handler,
                priority=1,
                propagate=False,
            )
        )
        client.add_message_handler(
            MessageHandlerBase(
                can_handle=lambda p: True,
                process=logging_handler,
                priority=2,
                propagate=True,
            )
        )
        client.add_message_handler(
            MessageHandlerBase(
                can_handle=lambda p: "response_code" in p.get("header", {}),
                process=response_handler,
                priority=3,
                propagate=False,
            )
        )

        await client.connect()
        await client.request("device1", "subsystem1", "path1")
        await asyncio.sleep(5)
        await client.disconnect()

    asyncio.run(main())