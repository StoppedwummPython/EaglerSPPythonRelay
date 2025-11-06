import asyncio
import configparser
import logging
import random
import string
import sys
import time
from collections import defaultdict
from enum import Enum
from io import BytesIO
from typing import Dict, List, Optional, Tuple

import websockets
from websockets.server import WebSocketServerProtocol

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(threadName)s/%(levelname)s][%(name)s]: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("EaglerSPRelay")


# --- Constants.java ---
class Constants:
    VERSION_NAME = "1.5"
    VERSION_BRAND = "Stoppedwumm"
    PROTOCOL_VERSION = 1


# --- Util.java ---
class Util:
    @staticmethod
    def millis():
        return int(time.time() * 1000)


# --- LoginState.java ---
class LoginState(Enum):
    INIT = 0
    SENT_ICE_CANDIDATE = 1
    RECIEVED_ICE_CANIDATE = 2
    SENT_DESCRIPTION = 3
    RECIEVED_DESCRIPTION = 4
    FINISHED = 5


# --- RelayPacket.java & other packet classes ---
PACKET_REGISTRY = {}
ID_REGISTRY = {}

class RelayPacket:
    """Base class for all relay packets."""

    def __init_subclass__(cls, packet_id: int, **kwargs):
        super().__init_subclass__(**kwargs)
        PACKET_REGISTRY[packet_id] = cls
        ID_REGISTRY[cls] = packet_id

    def read(self, stream: BytesIO):
        raise NotImplementedError

    def write(self, stream: BytesIO):
        raise NotImplementedError

    @staticmethod
    def read_packet(data: bytes) -> "RelayPacket":
        stream = BytesIO(data)
        packet_id = int.from_bytes(stream.read(1), "big")
        cls = PACKET_REGISTRY.get(packet_id)
        if not cls:
            raise IOError(f"Unknown packet type: {packet_id}")
        
        packet = cls()
        packet.read(stream)
        
        remaining = stream.read()
        if remaining:
            logger.warning(f"Packet type {packet_id} ({cls.__name__}) had {len(remaining)} remaining bytes")
            
        return packet

    @staticmethod
    def write_packet(packet: "RelayPacket") -> bytes:
        packet_id = ID_REGISTRY.get(type(packet))
        if packet_id is None:
            raise IOError(f"Unknown packet class: {type(packet).__name__}")
        
        stream = BytesIO()
        stream.write(packet_id.to_bytes(1, "big"))
        packet.write(stream)
        return stream.getvalue()

    def _read_ascii8(self, stream: BytesIO) -> str:
        length = int.from_bytes(stream.read(1), "big")
        return stream.read(length).decode("ascii")

    def _write_ascii8(self, stream: BytesIO, text: str):
        if text is None:
            stream.write(b'\x00')
            return
        data = text.encode("ascii")
        stream.write(len(data).to_bytes(1, "big"))
        stream.write(data)

    def _read_ascii16(self, stream: BytesIO) -> str:
        length = int.from_bytes(stream.read(2), "big")
        return stream.read(length).decode("ascii")

    def _write_ascii16(self, stream: BytesIO, text: str):
        if text is None:
            stream.write(b'\x00\x00')
            return
        data = text.encode("ascii")
        stream.write(len(data).to_bytes(2, "big"))
        stream.write(data)

    def _read_bytes16(self, stream: BytesIO) -> bytes:
        length = int.from_bytes(stream.read(2), "big")
        return stream.read(length)

    def _write_bytes16(self, stream: BytesIO, data: bytes):
        if data is None:
            stream.write(b'\x00\x00')
            return
        stream.write(len(data).to_bytes(2, "big"))
        stream.write(data)


class RelayPacket00Handshake(RelayPacket, packet_id=0x00):
    def __init__(self, conn_type=0, conn_version=Constants.PROTOCOL_VERSION, conn_code=""):
        self.connection_type = conn_type
        self.connection_version = conn_version
        self.connection_code = conn_code

    def read(self, stream: BytesIO):
        self.connection_type = int.from_bytes(stream.read(1), "big")
        self.connection_version = int.from_bytes(stream.read(1), "big")
        self.connection_code = self._read_ascii8(stream)

    def write(self, stream: BytesIO):
        stream.write(self.connection_type.to_bytes(1, "big"))
        stream.write(self.connection_version.to_bytes(1, "big"))
        self._write_ascii8(stream, self.connection_code)


class RelayPacket01ICEServers(RelayPacket, packet_id=0x01):
    class RelayType(Enum):
        NO_PASSWD = 'S'
        PASSWD = 'T'

    class RelayServer:
        def __init__(self, address, type, username, password):
            self.address = address
            self.type = type
            self.username = username
            self.password = password

    def __init__(self, servers=None):
        self.servers = servers if servers is not None else []

    def read(self, stream: BytesIO):
        count = int.from_bytes(stream.read(2), 'big')
        self.servers = []
        for _ in range(count):
            type_char = stream.read(1).decode('ascii')
            type_enum = self.RelayType(type_char)
            address = self._read_ascii16(stream)
            username = self._read_ascii8(stream)
            password = self._read_ascii8(stream)
            self.servers.append(self.RelayServer(address, type_enum, username, password))
            
    def write(self, stream: BytesIO):
        stream.write(len(self.servers).to_bytes(2, 'big'))
        for server in self.servers:
            stream.write(server.type.value.encode('ascii'))
            self._write_ascii16(stream, server.address)
            self._write_ascii8(stream, server.username)
            self._write_ascii8(stream, server.password)

class RelayPacket02NewClient(RelayPacket, packet_id=0x02):
    def __init__(self, client_id=""):
        self.client_id = client_id
    def read(self, stream): self.client_id = self._read_ascii8(stream)
    def write(self, stream): self._write_ascii8(stream, self.client_id)

class RelayPacket03ICECandidate(RelayPacket, packet_id=0x03):
    def __init__(self, peer_id="", candidate=b""):
        self.peer_id = peer_id
        self.candidate = candidate
    def read(self, stream): 
        self.peer_id = self._read_ascii8(stream)
        self.candidate = self._read_bytes16(stream)
    def write(self, stream):
        self._write_ascii8(stream, self.peer_id)
        self._write_bytes16(stream, self.candidate)

class RelayPacket04Description(RelayPacket, packet_id=0x04):
    def __init__(self, peer_id="", description=b""):
        self.peer_id = peer_id
        self.description = description
    def read(self, stream):
        self.peer_id = self._read_ascii8(stream)
        self.description = self._read_bytes16(stream)
    def write(self, stream):
        self._write_ascii8(stream, self.peer_id)
        self._write_bytes16(stream, self.description)

class RelayPacket05ClientSuccess(RelayPacket, packet_id=0x05):
    def __init__(self, client_id=""):
        self.client_id = client_id
    def read(self, stream): self.client_id = self._read_ascii8(stream)
    def write(self, stream): self._write_ascii8(stream, self.client_id)

class RelayPacket06ClientFailure(RelayPacket, packet_id=0x06):
    def __init__(self, client_id=""):
        self.client_id = client_id
    def read(self, stream): self.client_id = self._read_ascii8(stream)
    def write(self, stream): self._write_ascii8(stream, self.client_id)

class RelayPacket07LocalWorlds(RelayPacket, packet_id=0x07):
    class LocalWorld:
        def __init__(self, name, code):
            self.world_name = name
            self.world_code = code

    def __init__(self, worlds=None):
        self.worlds_list = worlds if worlds else []

    def read(self, stream):
        count = int.from_bytes(stream.read(1), "big")
        self.worlds_list = []
        for _ in range(count):
            name = self._read_ascii8(stream)
            code = self._read_ascii8(stream)
            self.worlds_list.append(self.LocalWorld(name, code))

    def write(self, stream):
        count = len(self.worlds_list)
        stream.write(count.to_bytes(1, "big"))
        for world in self.worlds_list:
            self._write_ascii8(stream, world.world_name)
            self._write_ascii8(stream, world.world_code)

class RelayPacket69Pong(RelayPacket, packet_id=0x69):
    def __init__(self, protocol_version=0, comment="", brand=""):
        self.protocol_version = protocol_version
        self.comment = comment
        self.brand = brand
    def read(self, stream):
        self.protocol_version = int.from_bytes(stream.read(1), 'big')
        self.comment = self._read_ascii8(stream)
        self.brand = self._read_ascii8(stream)
    def write(self, stream):
        stream.write(self.protocol_version.to_bytes(1, 'big'))
        self._write_ascii8(stream, self.comment)
        self._write_ascii8(stream, self.brand)

class RelayPacketFEDisconnectClient(RelayPacket, packet_id=0xFE):
    def __init__(self, client_id="", code=0, reason=""):
        self.client_id = client_id
        self.code = code
        self.reason = reason
    def read(self, stream):
        self.client_id = self._read_ascii8(stream)
        self.code = int.from_bytes(stream.read(1), 'big')
        self.reason = self._read_ascii16(stream)
    def write(self, stream):
        self._write_ascii8(stream, self.client_id)
        stream.write(self.code.to_bytes(1, 'big'))
        self._write_ascii16(stream, self.reason)

class RelayPacketFFErrorCode(RelayPacket, packet_id=0xFF):
    def __init__(self, code=0, desc=""):
        self.code = code
        self.desc = desc
    def read(self, stream):
        self.code = int.from_bytes(stream.read(1), 'big')
        self.desc = self._read_ascii16(stream)
    def write(self, stream):
        stream.write(self.code.to_bytes(1, 'big'))
        self._write_ascii16(stream, self.desc)

# --- EaglerSPRelayConfig.java ---
class EaglerSPRelayConfig:
    def __init__(self, filename="relayConfig.ini"):
        self.filename = filename
        self.config = configparser.ConfigParser()
        self.defaults = {
            "address": "0.0.0.0", "port": 6699, "code-length": 5,
            "code-chars": "abcdefghijklmnopqrstuvwxyz0123456789", "code-mix-case": False,
            "connections-per-ip": 128, "worlds-per-ip": 32,
            "world-ratelimit-enable": True, "world-ratelimit-period": 192,
            "world-ratelimit-limit": 32, "world-ratelimit-lockout-limit": 48,
            "world-ratelimit-lockout-duration": 600,
            "ping-ratelimit-enable": True, "ping-ratelimit-period": 256,
            "ping-ratelimit-limit": 128, "ping-ratelimit-lockout-limit": 192,
            "ping-ratelimit-lockout-duration": 300,
            "origin-whitelist": "", "enable-real-ip-header": False,
            "real-ip-header-name": "X-Real-IP", "show-local-worlds": True,
            "server-comment": "Eags. Shared World Relay"
        }

    def load(self):
        if not self.config.read(self.filename):
            logger.info(f"Creating default config file: {self.filename}")
            self.config['EaglerSPRelay'] = {k: str(v) for k, v in self.defaults.items()}
            self.save()
        
        section = self.config['EaglerSPRelay']
        needs_save = False
        for key, value in self.defaults.items():
            if key not in section:
                section[key] = str(value)
                needs_save = True
        if needs_save:
            logger.info(f"Updating config file with new keys: {self.filename}")
            self.save()

    def save(self):
        with open(self.filename, 'w') as configfile:
            self.config.write(configfile)

    def get(self, key): return self.config.get('EaglerSPRelay', key)
    def getint(self, key): return self.config.getint('EaglerSPRelay', key)
    def getboolean(self, key): return self.config.getboolean('EaglerSPRelay', key)
    
    def generate_code(self):
        chars = self.get('code-chars')
        length = self.getint('code-length')
        code = ''.join(random.choice(chars) for _ in range(length))
        if self.getboolean('code-mix-case'):
            code = ''.join(c.upper() if random.choice([True, False]) else c.lower() for c in code)
        return code

    def is_origin_whitelisted(self, origin: Optional[str]) -> bool:
        whitelist_str = self.get('origin-whitelist')
        if not whitelist_str:
            return True
        whitelist = [item.strip().lower() for item in whitelist_str.split(';') if item.strip()]
        
        if origin is None:
            origin_domain = "null"
        else:
            origin_domain = origin.lower()
            if origin_domain.startswith("http://"): origin_domain = origin_domain[7:]
            elif origin_domain.startswith("https://"): origin_domain = origin_domain[8:]
        
        for entry in whitelist:
            if entry.startswith("*") and origin_domain.endswith(entry[1:]):
                return True
            if entry == origin_domain:
                return True
        return False


# --- RateLimiter.java ---
class RateLimiter:
    class RateLimit(Enum):
        NONE = 0
        LIMIT = 1
        LIMIT_NOW_LOCKOUT = 2
        LOCKOUT = 3

    class RateLimitEntry:
        def __init__(self, period, limit_val):
            self.period = period
            self.limit_val = limit_val
            self.timer = Util.millis()
            self.count = 0
            self.locked_timer = 0
            self.locked = False
        
        def update(self, lockout_duration):
            millis = Util.millis()
            if self.locked:
                if millis - self.locked_timer > lockout_duration:
                    self.locked = False
                    self.count = 0
                    self.timer = millis
            else:
                p = self.period / self.limit_val
                while millis - self.timer > p and self.count > 0:
                    self.timer += p
                    self.count -= 1

    def __init__(self, period, limit, lockout_limit, lockout_duration):
        self.period = period * 1000
        self.limit = limit
        self.lockout_limit = lockout_limit
        self.lockout_duration = lockout_duration * 1000
        self.limiters = defaultdict(lambda: self.RateLimitEntry(self.period, self.limit))
        self.lock = asyncio.Lock()

    async def check_and_limit(self, addr: str) -> RateLimit:
        async with self.lock:
            entry = self.limiters[addr]
            entry.update(self.lockout_duration)
            if entry.locked:
                return self.RateLimit.LOCKOUT
            
            entry.count += 1
            if entry.count >= self.lockout_limit:
                entry.locked = True
                entry.locked_timer = Util.millis()
                return self.RateLimit.LIMIT_NOW_LOCKOUT
            elif entry.count > self.limit:
                return self.RateLimit.LIMIT
            return self.RateLimit.NONE
    
    async def update_cleanup(self):
        async with self.lock:
            keys_to_del = [addr for addr, entry in self.limiters.items() if not entry.locked and entry.count == 0]
            for key in keys_to_del:
                del self.limiters[key]
    
    async def reset(self):
        async with self.lock:
            self.limiters.clear()

# --- EaglerSPRelayConfigRelayList.java ---
class RelayList:
    relay_servers: List[RelayPacket01ICEServers.RelayServer] = []

    @staticmethod
    def load_relays(filename="relays.txt"):
        try:
            with open(filename, 'r') as f:
                current_type = None
                current_url, current_user, current_pass = None, None, None
                
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    
                    is_stun = line.upper() in ("[STUN]", "[NO_PASSWD]")
                    is_turn = line.upper() in ("[TURN]", "[PASSWD]")

                    if is_stun or is_turn:
                        if current_type and current_url:
                            RelayList.relay_servers.append(RelayPacket01ICEServers.RelayServer(
                                current_url, current_type, current_user, current_pass
                            ))
                        current_type = RelayPacket01ICEServers.RelayType.NO_PASSWD if is_stun else RelayPacket01ICEServers.RelayType.PASSWD
                        current_url, current_user, current_pass = None, None, None
                    elif '=' in line and current_type:
                        key, value = line.split('=', 1)
                        key, value = key.strip().lower(), value.strip()
                        if key == 'url': current_url = value
                        elif key == 'username': current_user = value
                        elif key == 'password': current_pass = value

                if current_type and current_url:
                    RelayList.relay_servers.append(RelayPacket01ICEServers.RelayServer(
                        current_url, current_type, current_user, current_pass
                    ))

            logger.info(f"Loaded {len(RelayList.relay_servers)} STUN/TURN servers from {filename}")
        except FileNotFoundError:
            logger.warning(f"{filename} not found, creating a default one.")
            with open(filename, 'w') as f:
                f.write("# Add your STUN/TURN servers here\n")
                f.write("# Example for a STUN server (no password)\n")
                f.write("[STUN]\n")
                f.write("url=stun:stun.l.google.com:19302\n")
            RelayList.load_relays(filename)

# Forward declarations
class EaglerSPServer: pass
class EaglerSPClient: pass

# --- EaglerSPServer.java ---
class EaglerSPServer:
    def __init__(self, socket: WebSocketServerProtocol, code: str, server_name: str, server_address: str):
        self.socket = socket
        self.code = code
        self.clients: Dict[str, EaglerSPClient] = {}
        self.server_address = server_address
        self.server_hidden = False
        
        if server_name.endswith(";1"):
            self.server_hidden = True
            self.server_name = server_name[:-2]
        elif server_name.endswith(";0"):
            self.server_hidden = False
            self.server_name = server_name[:-2]
        else:
            self.server_name = server_name
            
    async def send(self, packet: RelayPacket):
        if self.socket.open:
            try:
                await self.socket.send(RelayPacket.write_packet(packet))
            except websockets.ConnectionClosed:
                pass
            except Exception as e:
                logger.debug(f"Error sending data to server {self.server_address}: {e}")
                await self.socket.close()
    
    async def handle_packet(self, packet: RelayPacket):
        client_id_attr = getattr(packet, 'peer_id', getattr(packet, 'client_id', None))
        client = self.clients.get(client_id_attr) if client_id_attr else None

        if client_id_attr and not client:
            logger.warning(f"Server {self.code} received packet for unknown client {client_id_attr}")
            return

        if isinstance(packet, RelayPacket03ICECandidate):
            if client.state == LoginState.SENT_ICE_CANDIDATE:
                client.state = LoginState.RECIEVED_ICE_CANIDATE
                await client.handle_server_ice_candidate(packet)
        elif isinstance(packet, RelayPacket04Description):
            if client.state == LoginState.SENT_DESCRIPTION:
                client.state = LoginState.RECIEVED_DESCRIPTION
                await client.handle_server_description(packet)
        elif isinstance(packet, RelayPacketFEDisconnectClient):
             await client.handle_server_disconnect(packet)
        else:
            logger.warning(f"Server {self.code} received unhandled packet: {type(packet).__name__}")


# --- EaglerSPClient.java ---
class EaglerSPClient:
    CLIENT_CODE_LENGTH = 16
    CLIENT_CODE_CHARS = string.ascii_uppercase + string.digits

    def __init__(self, socket: WebSocketServerProtocol, server: EaglerSPServer, client_id: str, address: str):
        self.socket = socket
        self.server = server
        self.id = client_id
        self.address = address
        self.created_on = Util.millis()
        self.state = LoginState.INIT
        self.server_notified_of_close = False

    @staticmethod
    def generate_client_id():
        return ''.join(random.choice(EaglerSPClient.CLIENT_CODE_CHARS) for _ in range(EaglerSPClient.CLIENT_CODE_LENGTH))

    async def send(self, packet: RelayPacket):
        if self.socket.open:
            try:
                await self.socket.send(RelayPacket.write_packet(packet))
            except websockets.ConnectionClosed:
                pass
            except Exception as e:
                logger.debug(f"Error sending data to client {self.id}: {e}")
                await self.disconnect(4, "Internal Server Error")

    async def disconnect(self, code: int, reason: str):
        if not self.server_notified_of_close and code != 0:
            self.server_notified_of_close = True
            await self.server.send(RelayPacketFEDisconnectClient(self.id, code, reason))
        
        if self.socket.open:
            packet = RelayPacketFEDisconnectClient(self.id, code, reason)
            try:
                await self.send(packet)
            finally:
                await self.socket.close()
        
    async def handle_packet(self, packet: RelayPacket):
        if isinstance(packet, RelayPacket04Description):
            if self.state == LoginState.INIT:
                self.state = LoginState.SENT_DESCRIPTION
                await self.server.send(RelayPacket04Description(self.id, packet.description))
            else:
                await self.disconnect(3, f"Invalid state for Description: {self.state.name}")
        elif isinstance(packet, RelayPacket03ICECandidate):
            if self.state == LoginState.RECIEVED_DESCRIPTION:
                self.state = LoginState.SENT_ICE_CANDIDATE
                await self.server.send(RelayPacket03ICECandidate(self.id, packet.candidate))
            else:
                await self.disconnect(3, f"Invalid state for ICECandidate: {self.state.name}")
        elif isinstance(packet, RelayPacket05ClientSuccess):
            if self.state == LoginState.RECIEVED_ICE_CANIDATE:
                self.state = LoginState.FINISHED
                await self.server.send(RelayPacket05ClientSuccess(self.id))
                await self.disconnect(0, "Successful connection")
            else:
                await self.disconnect(3, f"Invalid state for Success: {self.state.name}")
        elif isinstance(packet, RelayPacket06ClientFailure):
            if self.state == LoginState.RECIEVED_ICE_CANIDATE:
                self.state = LoginState.FINISHED
                await self.server.send(RelayPacket06ClientFailure(self.id))
                await self.disconnect(1, "Failed connection")
            else:
                await self.disconnect(3, f"Invalid state for Failure: {self.state.name}")

    async def handle_server_ice_candidate(self, packet: RelayPacket03ICECandidate):
        await self.send(RelayPacket03ICECandidate("", packet.candidate))

    async def handle_server_description(self, packet: RelayPacket04Description):
        await self.send(RelayPacket04Description("", packet.description))
        
    async def handle_server_disconnect(self, packet: RelayPacketFEDisconnectClient):
        await self.disconnect(packet.code, packet.reason)


# --- EaglerSPRelay.java ---
class EaglerSPRelay:
    def __init__(self):
        self.config = EaglerSPRelayConfig()
        self.config.load()
        self.ping_ratelimiter, self.world_ratelimiter = None, None
        
        if self.config.getboolean('ping-ratelimit-enable'):
            self.ping_ratelimiter = RateLimiter(
                self.config.getint('ping-ratelimit-period'), self.config.getint('ping-ratelimit-limit'),
                self.config.getint('ping-ratelimit-lockout-limit'), self.config.getint('ping-ratelimit-lockout-duration')
            )
        if self.config.getboolean('world-ratelimit-enable'):
             self.world_ratelimiter = RateLimiter(
                self.config.getint('world-ratelimit-period'), self.config.getint('world-ratelimit-limit'),
                self.config.getint('world-ratelimit-lockout-limit'), self.config.getint('world-ratelimit-lockout-duration')
            )
        
        RelayList.load_relays()
        self.client_ids, self.server_codes = {}, {}
        self.pending_conns, self.client_conns, self.server_conns = {}, {}, {}
        self.client_addr_sets = defaultdict(list)
        self.server_addr_sets = defaultdict(list)
        self.state_lock = asyncio.Lock()

    def get_real_ip(self, websocket: WebSocketServerProtocol) -> str:
        if self.config.getboolean('enable-real-ip-header'):
            header_name = self.config.get('real-ip-header-name')
            if header_name in websocket.request_headers:
                return websocket.request_headers[header_name]
        return websocket.remote_address[0]

    async def handle_connection(self, websocket: WebSocketServerProtocol, path: str):
        origin = websocket.request_headers.get('Origin')
        if not self.config.is_origin_whitelisted(origin):
            logger.warning(f"Blocked connection from disallowed origin {origin}")
            return

        addr = self.get_real_ip(websocket)
        
        async with self.state_lock:
            total_conns = len(self.client_addr_sets[addr]) + sum(1 for _, a in self.pending_conns.values() if a == addr)
            if total_conns >= self.config.getint('connections-per-ip'):
                 logger.debug(f"[{addr}] Rejected: Too many connections are open")
                 return
            self.pending_conns[websocket] = (Util.millis(), addr)

        try:
            async for message in websocket:
                await self.on_message(websocket, message)
        except websockets.exceptions.ConnectionClosedError:
             logger.debug(f"Connection from {addr} closed unexpectedly.")
        finally:
            await self.on_close(websocket)
            
    async def on_message(self, websocket: WebSocketServerProtocol, message: bytes):
        is_pending = False
        async with self.state_lock:
            if websocket in self.pending_conns:
                is_pending = True
                del self.pending_conns[websocket]

        if is_pending:
            await self.handle_handshake(websocket, message)
        else:
            await self.handle_relay(websocket, message)

    async def handle_handshake(self, websocket: WebSocketServerProtocol, message: bytes):
        addr = self.get_real_ip(websocket)
        try:
            packet = RelayPacket.read_packet(message)
            if not isinstance(packet, RelayPacket00Handshake):
                await websocket.send(RelayPacket.write_packet(RelayPacketFFErrorCode(3, "Unexpected Init Packet")))
                await websocket.close()
                return

            if packet.connection_version != Constants.PROTOCOL_VERSION:
                err = "Outdated Client!" if packet.connection_version < 1 else "Outdated Server!"
                await websocket.send(RelayPacket.write_packet(RelayPacketFFErrorCode(1, err)))
                await websocket.close()
                return

            conn_type = packet.connection_type
            if conn_type == 1: # Server
                if self.world_ratelimiter and await self.world_ratelimiter.check_and_limit(addr) != RateLimiter.RateLimit.NONE:
                    logger.debug(f"[{addr}] Rejected: World creation rate limited")
                    await websocket.close()
                    return
                
                async with self.state_lock:
                    if len(self.server_addr_sets[addr]) >= self.config.getint('worlds-per-ip'):
                        logger.debug(f"[{addr}] Rejected: Too many worlds open on this IP")
                        await websocket.close()
                        return

                    code = self.config.generate_code()
                    while code in self.server_codes:
                        code = self.config.generate_code()
                    
                    server = EaglerSPServer(websocket, code, packet.connection_code, addr)
                    self.server_codes[code], self.server_conns[websocket] = server, server
                    self.server_addr_sets[addr].append(server)

                packet.connection_code = code
                await websocket.send(RelayPacket.write_packet(packet))
                await server.send(RelayPacket01ICEServers(RelayList.relay_servers))
                logger.info(f"Server connected from {addr}, assigned code: {code}")

            elif conn_type == 2: # Client
                if self.ping_ratelimiter and await self.ping_ratelimiter.check_and_limit(addr) != RateLimiter.RateLimit.NONE:
                    logger.debug(f"[{addr}] Rejected: Client connection rate limited")
                    await websocket.close()
                    return
                
                code = packet.connection_code if self.config.getboolean('code-mix-case') else packet.connection_code.lower()
                async with self.state_lock:
                    server = self.server_codes.get(code)
                    if not server:
                        await websocket.send(RelayPacket.write_packet(RelayPacketFFErrorCode(5, "Invalid code")))
                        await websocket.close()
                        return

                    client_id = EaglerSPClient.generate_client_id()
                    while client_id in self.client_ids: client_id = EaglerSPClient.generate_client_id()
                    
                    client = EaglerSPClient(websocket, server, client_id, addr)
                    self.client_ids[client_id], self.client_conns[websocket] = client, client
                    self.client_addr_sets[addr].append(client)
                    server.clients[client_id] = client

                packet.connection_code = client.id
                await websocket.send(RelayPacket.write_packet(packet))
                await server.send(RelayPacket02NewClient(client.id))
                await client.send(RelayPacket01ICEServers(RelayList.relay_servers))
                logger.info(f"Client from {addr} connecting to server {code}")

            elif conn_type == 3: # Ping
                await websocket.send(RelayPacket.write_packet(RelayPacket69Pong(
                    Constants.PROTOCOL_VERSION, self.config.get('server-comment'), Constants.VERSION_BRAND)))
                await websocket.close() # Ping connections are short-lived

            elif conn_type == 4: # Poll local worlds
                if self.config.getboolean('show-local-worlds'):
                    async with self.state_lock:
                        servers = self.server_addr_sets.get(addr, [])
                        worlds = [RelayPacket07LocalWorlds.LocalWorld(s.server_name, s.code) for s in servers if not s.server_hidden]
                    await websocket.send(RelayPacket.write_packet(RelayPacket07LocalWorlds(worlds)))
                else:
                    await websocket.send(RelayPacket.write_packet(RelayPacket07LocalWorlds([])))
                await websocket.close() # Poll connections are short-lived
        except Exception:
            logger.exception(f"Unhandled error during handshake from {addr}:")
            if websocket.open:
                await websocket.close()

    async def handle_relay(self, websocket: WebSocketServerProtocol, message: bytes):
        addr = self.get_real_ip(websocket)
        try:
            packet = RelayPacket.read_packet(message)
            async with self.state_lock:
                server = self.server_conns.get(websocket)
                client = self.client_conns.get(websocket)
            
            if server: await server.handle_packet(packet)
            elif client: await client.handle_packet(packet)
        except Exception:
            logger.exception(f"Unhandled error during relay from {addr}:")
            if websocket.open:
                await websocket.close()

    async def on_close(self, websocket: WebSocketServerProtocol):
        addr = self.get_real_ip(websocket)
        async with self.state_lock:
            server = self.server_conns.pop(websocket, None)
            client = self.client_conns.pop(websocket, None)
            self.pending_conns.pop(websocket, None)
        
        if server:
            logger.info(f"Server {server.code} from {addr} disconnected.")
            async with self.state_lock:
                self.server_codes.pop(server.code, None)
                if addr in self.server_addr_sets and server in self.server_addr_sets[addr]:
                    self.server_addr_sets[addr].remove(server)
                clients_to_disconnect = list(server.clients.values())
            for cl in clients_to_disconnect:
                await cl.disconnect(6, "Server disconnected")

        if client:
            logger.info(f"Client {client.id} from {addr} disconnected.")
            async with self.state_lock:
                self.client_ids.pop(client.id, None)
                if addr in self.client_addr_sets and client in self.client_addr_sets[addr]:
                    self.client_addr_sets[addr].remove(client)
                
                server = client.server
                if server and client.id in server.clients:
                    del server.clients[client.id]
            
            if server and not client.server_notified_of_close:
                await server.send(RelayPacketFEDisconnectClient(client.id, 255, "End of stream"))

    async def tick_loop(self):
        while True:
            await asyncio.sleep(1)
            now = Util.millis()
            async with self.state_lock:
                pending_to_close = [ws for ws, (t, _) in self.pending_conns.items() if now - t > 2000]
                clients_to_close = [c for c in self.client_conns.values() if now - c.created_on > 15000 and c.state != LoginState.FINISHED]

            for ws in pending_to_close:
                addr = self.get_real_ip(ws)
                logger.debug(f"Closing pending connection {addr} due to timeout")
                await ws.close()
            for c in clients_to_close:
                await c.disconnect(2, "Took too long to connect")

            if self.ping_ratelimiter: await self.ping_ratelimiter.update_cleanup()
            if self.world_ratelimiter: await self.world_ratelimiter.update_cleanup()

    async def run(self):
        host = self.config.get('address')
        port = self.config.getint('port')
        
        asyncio.create_task(self.tick_loop())
        
        async with websockets.serve(self.handle_connection, host, port) as server:
            logger.info(f"EaglerSPRelay listening on {host}:{port}")
            logger.info("Type 'stop' to exit, 'reset' to clear ratelimits")
            await server.wait_closed()


async def main():
    relay = EaglerSPRelay()
    loop = asyncio.get_running_loop()

    server_task = asyncio.create_task(relay.run())

    if sys.platform == "win32":
        def stdin_reader():
            while True:
                try: line = sys.stdin.readline().strip().lower()
                except Exception: break
                if line in ("stop", "end"):
                    logger.info("Shutting down...")
                    loop.call_soon_threadsafe(loop.stop)
                    break
                elif line == "reset":
                    logger.info("Clearing all ratelimits")
                    if relay.ping_ratelimiter: asyncio.run_coroutine_threadsafe(relay.ping_ratelimiter.reset(), loop)
                    if relay.world_ratelimiter: asyncio.run_coroutine_threadsafe(relay.world_ratelimiter.reset(), loop)
                else: logger.info("Unknown command. Available: 'stop', 'reset'")
        import threading
        threading.Thread(target=stdin_reader, daemon=True).start()
    else:
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        async def handle_commands():
            while True:
                line = await reader.readline()
                command = line.decode().strip().lower()
                if command in ("stop", "end"):
                    logger.info("Shutting down...")
                    loop.stop()
                    break
                elif command == "reset":
                    logger.info("Clearing all ratelimits")
                    if relay.ping_ratelimiter: await relay.ping_ratelimiter.reset()
                    if relay.world_ratelimiter: await relay.world_ratelimiter.reset()
                else: logger.info("Unknown command. Available: 'stop', 'reset'")
        asyncio.create_task(handle_commands())
    
    await server_task


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
        logger.info("Server stopped.")