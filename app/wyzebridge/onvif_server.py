"""
ONVIF Server implementation for Wyze Bridge.
Exposes Wyze cameras as ONVIF-compliant devices.
"""

import hashlib
import logging
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
import struct
import socketserver
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import netifaces
from lxml import etree
from spyne import Application, ServiceBase, rpc, String, Integer, Boolean, DateTime
from spyne.decorator import srpc
from spyne.model.complex import ComplexModel
from spyne.protocol.soap import Soap11
from spyne.server.wsgi import WsgiApplication

from wyzebridge.config import BRIDGE_IP, RTSP_URL
from wyzebridge.logging import logger
from wyzebridge.onvif_ptz import PTZService
from wyzecam.api_models import WyzeCamera


class OnvifCamera:
    """Represents a Wyze camera in ONVIF format."""
    
    def __init__(self, wyze_cam: WyzeCamera, stream_uri: str, stream_obj=None):
        self.wyze_cam = wyze_cam
        self.stream_uri = stream_uri
        self.stream_obj = stream_obj
        self.uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, wyze_cam.mac))
        self.profiles = self._create_profiles()
        
    def _create_profiles(self) -> List[Dict]:
        """Create media profiles for the camera using actual stream settings."""
        profiles = []
        
        # Get actual stream quality settings
        actual_width, actual_height, actual_bitrate, actual_fps = self._get_actual_stream_settings()
        
        # Main profile with actual stream settings
        profiles.append({
            'token': f'{self.wyze_cam.name_uri}_main',
            'name': f'{self.wyze_cam.nickname} Main',
            'video_source': {
                'token': f'video_{self.wyze_cam.name_uri}',
                'resolution': {'width': actual_width, 'height': actual_height},
                'framerate': actual_fps,
            },
            'video_encoder': {
                'token': f'encoder_{self.wyze_cam.name_uri}',
                'encoding': 'H264',
                'resolution': {'width': actual_width, 'height': actual_height},
                'bitrate': actual_bitrate,
                'framerate': actual_fps,
            },
            'audio_source': {
                'token': f'audio_{self.wyze_cam.name_uri}',
                'channels': 1,
            },
            'ptz': self.wyze_cam.is_pan_cam,
        })
        
        return profiles
    
    def _get_actual_stream_settings(self) -> tuple:
        """Get actual stream resolution, bitrate, and FPS from stream configuration."""
        try:
            # Try to get settings from stream object
            if self.stream_obj and hasattr(self.stream_obj, 'options'):
                options = self.stream_obj.options
                
                # Get actual quality settings
                quality = getattr(options, 'quality', 'hd60').lower()
                bitrate = getattr(options, 'bitrate', 60)
                
                # Parse quality to get resolution
                if 'sd' in quality:
                    width, height = 640, 480
                elif 'hd720' in quality or quality.startswith('hd7'):
                    width, height = 1280, 720
                elif 'hd1080' in quality or quality.startswith('hd10'):
                    width, height = 1920, 1080
                elif 'hd360' in quality or quality.startswith('hd3'):
                    width, height = 640, 360
                else:
                    # Default to SD for low bitrates
                    width, height = (640, 480) if bitrate < 500 else (1280, 720)
                    
                # Adjust for 2K cameras
                if self.wyze_cam.is_2k and quality not in ['sd', 'hd360']:
                    width, height = min(width * 1.5, 2560), min(height * 1.5, 1440)
                    
                # Get actual FPS from camera or stream
                fps = self._get_actual_fps()
                
                logger.info(f"[ONVIF] {self.wyze_cam.nickname} actual settings: {width}x{height}, {bitrate}kbps, {fps}fps")
                return int(width), int(height), bitrate, fps
                
        except Exception as e:
            logger.warning(f"[ONVIF] Could not get actual stream settings for {self.wyze_cam.nickname}: {e}")
            
        # Fallback to conservative defaults for compatibility
        actual_fps = self._get_actual_fps()
        logger.info(f"[ONVIF] {self.wyze_cam.nickname} using fallback settings: 640x480, 500kbps, {actual_fps}fps")
        return 640, 480, 500, actual_fps
    
    def _get_actual_fps(self) -> int:
        """Get actual FPS from camera info or stream settings."""
        try:
            # Method 1: Try to get from camera info (most accurate)
            if hasattr(self.wyze_cam, 'camera_info') and self.wyze_cam.camera_info:
                video_param = self.wyze_cam.camera_info.get('videoParm', {})
                if video_param and 'fps' in video_param:
                    fps = int(video_param['fps'])
                    logger.debug(f"[ONVIF] Got FPS from camera_info: {fps}")
                    return fps
                    
            # Method 2: Try to get from stream object if available
            if self.stream_obj and hasattr(self.stream_obj, 'camera'):
                stream_cam = self.stream_obj.camera
                if hasattr(stream_cam, 'camera_info') and stream_cam.camera_info:
                    video_param = stream_cam.camera_info.get('videoParm', {})
                    if video_param and 'fps' in video_param:
                        fps = int(video_param['fps'])
                        logger.debug(f"[ONVIF] Got FPS from stream camera_info: {fps}")
                        return fps
                        
            # Method 3: Check for common environment variables
            from wyzebridge.bridge_utils import env_cam
            force_fps = env_cam("FORCE_FPS", self.wyze_cam.name_uri, "0")
            if force_fps and force_fps != "0":
                fps = int(force_fps)
                logger.debug(f"[ONVIF] Got FPS from FORCE_FPS: {fps}")
                return fps
                
            # Method 4: Infer from quality/bitrate (lower quality typically means lower FPS)
            if self.stream_obj and hasattr(self.stream_obj, 'options'):
                bitrate = getattr(self.stream_obj.options, 'bitrate', 60)
                quality = getattr(self.stream_obj.options, 'quality', 'hd60').lower()
                
                if bitrate < 100 or 'sd' in quality:
                    fps = 10  # Low quality usually 10fps
                elif bitrate < 500 or '360' in quality:
                    fps = 15  # Medium-low quality 15fps
                else:
                    fps = 20  # Higher quality 20fps
                    
                logger.debug(f"[ONVIF] Inferred FPS from quality/bitrate: {fps}")
                return fps
                
        except Exception as e:
            logger.warning(f"[ONVIF] Error getting actual FPS: {e}")
            
        # Final fallback - assume low FPS for poor quality streams
        logger.debug(f"[ONVIF] Using fallback FPS: 15")
        return 15


class WSDiscoveryServer:
    """WS-Discovery server for ONVIF device discovery."""
    
    def __init__(self, cameras: Dict[str, OnvifCamera], http_port: int = 8080):
        self.cameras = cameras
        self.http_port = http_port
        self.running = False
        self.sock = None
        self.thread = None
        self.announce_thread = None
        
    def start(self):
        """Start the discovery server."""
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        
        # Start periodic announcements for UniFi compatibility
        self.announce_thread = threading.Thread(target=self._announce_periodically, daemon=True)
        self.announce_thread.start()
        
    def stop(self):
        """Stop the discovery server."""
        self.running = False
        if self.sock:
            self.sock.close()
            
    def _announce_periodically(self):
        """Send periodic Hello announcements for UniFi discovery."""
        import time
        
        time.sleep(5)  # Wait for server to fully start
        
        while self.running:
            try:
                self._send_hello_announcements()
                time.sleep(30)  # Announce every 30 seconds
            except Exception as e:
                logger.debug(f"[ONVIF] Announcement error: {e}")
                time.sleep(5)
                
    def _send_hello_announcements(self):
        """Send Hello announcements to multicast and broadcast."""
        for cam_name, camera in self.cameras.items():
            try:
                hello_msg = self._create_hello_announcement(camera)
                
                # Send to multicast
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                
                # Multicast
                sock.sendto(hello_msg, ('239.255.255.250', 3702))
                
                # Broadcast to local subnet
                sock.sendto(hello_msg, ('<broadcast>', 3702))
                sock.close()
                
                logger.debug(f"[ONVIF] 📢 Hello announcement sent for {cam_name}")
                
            except Exception as e:
                logger.debug(f"[ONVIF] Failed to send Hello for {cam_name}: {e}")
                
    def _create_hello_announcement(self, camera: OnvifCamera) -> bytes:
        """Create Hello announcement message."""
        message_id = str(uuid.uuid4())
        bridge_ip = BRIDGE_IP or self._get_local_ip()
        
        hello = f'''<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope
    xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
    xmlns:wsa="http://www.w3.org/2005/08/addressing"
    xmlns:wsd="http://docs.oasis-open.org/ws-dd/ns/discovery/2009/01"
    xmlns:dn="http://www.onvif.org/ver10/network/wsdl"
    xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
    <soap:Header>
        <wsa:Action soap:mustUnderstand="1">http://docs.oasis-open.org/ws-dd/ns/discovery/2009/01/Hello</wsa:Action>
        <wsa:MessageID>urn:uuid:{message_id}</wsa:MessageID>
        <wsa:To soap:mustUnderstand="1">urn:docs-oasis-open-org:ws-dd:discovery:2009:01</wsa:To>
        <wsa:AppSequence InstanceId="1" MessageNumber="1"/>
    </soap:Header>
    <soap:Body>
        <wsd:Hello>
            <wsa:EndpointReference>
                <wsa:Address>urn:uuid:{camera.uuid}</wsa:Address>
            </wsa:EndpointReference>
            <wsd:Types>dn:NetworkVideoTransmitter tds:Device</wsd:Types>
            <wsd:Scopes>onvif://www.onvif.org/type/NetworkVideoTransmitter onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/hardware/{camera.wyze_cam.product_model} onvif://www.onvif.org/name/{camera.wyze_cam.nickname.replace(' ', '_')} onvif://www.onvif.org/location/</wsd:Scopes>
            <wsd:XAddrs>http://{bridge_ip}:{self.http_port}/onvif/device_service</wsd:XAddrs>
            <wsd:MetadataVersion>1</wsd:MetadataVersion>
        </wsd:Hello>
    </soap:Body>
</soap:Envelope>'''
        
        return hello.encode('utf-8')
            
    def _run(self):
        """Run the discovery service."""
        logger.info("[ONVIF] ====== WS-DISCOVERY DEBUG ======")
        logger.info(f"[ONVIF] Starting WS-Discovery with {len(self.cameras)} cameras")
        
        if not self.cameras:
            logger.error("[ONVIF] No cameras available for discovery! Discovery server will not be useful.")
            return
            
        try:
            # Create UDP socket for multicast
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            
            # Enable broadcast (may help in Docker)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            
            # Bind to the multicast address
            self.sock.bind(('', 3702))
            logger.info("[ONVIF] Socket bound to port 3702")
            
            # Join multicast group
            mreq = struct.pack("4sl", socket.inet_aton('239.255.255.250'), socket.INADDR_ANY)
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            logger.info("[ONVIF] Joined multicast group 239.255.255.250")
            
            # Set timeout for graceful shutdown
            self.sock.settimeout(2.0)
            
            logger.info(f"[ONVIF] WS-Discovery server started successfully on port 3702")
            logger.info(f"[ONVIF] Listening for probes from UniFi controllers...")
            
            probe_count = 0
            while self.running:
                try:
                    data, addr = self.sock.recvfrom(4096)
                    probe_count += 1
                    logger.info(f"[ONVIF] PROBE #{probe_count} received from {addr[0]}:{addr[1]}")
                    self._handle_probe(data, addr)
                except socket.timeout:
                    if probe_count == 0 and self.running:
                        logger.debug("[ONVIF] No probes received yet (this is normal)...")
                    continue
                except Exception as e:
                    if self.running:
                        logger.error(f"[ONVIF] Discovery error: {e}")
                        
        except Exception as e:
            logger.error(f"[ONVIF] Failed to start WS-Discovery: {e}")
            logger.error("[ONVIF] This could be due to Docker network restrictions or port conflicts")
            
    def _handle_probe(self, data: bytes, addr):
        """Handle probe requests."""
        try:
            message = data.decode('utf-8')
            logger.debug(f"[ONVIF] Raw probe data: {message[:500]}...")
            
            # Simple XML parsing to check for Probe
            if 'Probe>' not in message:
                logger.debug("[ONVIF] Not a Probe message, ignoring")
                return
                
            logger.info(f"[ONVIF] 🎯 Valid Probe received from {addr[0]}:{addr[1]}")
            
            # Extract MessageID for RelatesTo
            message_id = None
            try:
                root = etree.fromstring(data)
                msg_elem = root.find('.//{http://www.w3.org/2005/08/addressing}MessageID')
                if msg_elem is not None:
                    message_id = msg_elem.text.replace('urn:uuid:', '')
                    logger.debug(f"[ONVIF] Extracted MessageID: {message_id}")
            except Exception as xml_error:
                logger.warning(f"[ONVIF] XML parse error: {xml_error}")
                message_id = str(uuid.uuid4())
            
            if not message_id:
                message_id = str(uuid.uuid4())
            
            # Send probe match for each camera
            cameras_responded = 0
            for cam_name, camera in self.cameras.items():
                try:
                    response = self._create_probe_match(camera, message_id)
                    
                    # Send response back to sender
                    response_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    response_sock.sendto(response, addr)
                    response_sock.close()
                    cameras_responded += 1
                    
                    logger.info(f"[ONVIF] ✅ ProbeMatch sent for {cam_name} ({camera.wyze_cam.nickname})")
                    logger.debug(f"[ONVIF] Response preview: {response.decode('utf-8')[:300]}...")
                except Exception as send_error:
                    logger.error(f"[ONVIF] Failed to send ProbeMatch for {cam_name}: {send_error}")
                    
            logger.info(f"[ONVIF] 📡 Sent {cameras_responded} ProbeMatch responses to {addr[0]}:{addr[1]}")
                
        except Exception as e:
            logger.error(f"[ONVIF] Probe handling error: {e}")
            logger.error(f"[ONVIF] Raw data length: {len(data)} bytes")
            
    def _create_probe_match(self, camera: OnvifCamera, relates_to: str) -> bytes:
        """Create UniFi-compatible ProbeMatch response."""
        message_id = str(uuid.uuid4())
        bridge_ip = BRIDGE_IP or self._get_local_ip()
        
        # UniFi expects specific namespace prefixes and format
        response = f'''<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope
    xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
    xmlns:wsa="http://www.w3.org/2005/08/addressing"
    xmlns:wsd="http://docs.oasis-open.org/ws-dd/ns/discovery/2009/01"
    xmlns:dn="http://www.onvif.org/ver10/network/wsdl"
    xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
    <soap:Header>
        <wsa:Action soap:mustUnderstand="1">http://docs.oasis-open.org/ws-dd/ns/discovery/2009/01/ProbeMatches</wsa:Action>
        <wsa:MessageID>urn:uuid:{message_id}</wsa:MessageID>
        <wsa:RelatesTo>urn:uuid:{relates_to}</wsa:RelatesTo>
        <wsa:To soap:mustUnderstand="1">http://www.w3.org/2005/08/addressing/anonymous</wsa:To>
        <wsa:AppSequence InstanceId="1" MessageNumber="1"/>
    </soap:Header>
    <soap:Body>
        <wsd:ProbeMatches>
            <wsd:ProbeMatch>
                <wsa:EndpointReference>
                    <wsa:Address>urn:uuid:{camera.uuid}</wsa:Address>
                </wsa:EndpointReference>
                <wsd:Types>dn:NetworkVideoTransmitter tds:Device</wsd:Types>
                <wsd:Scopes>onvif://www.onvif.org/type/NetworkVideoTransmitter onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/hardware/{camera.wyze_cam.product_model} onvif://www.onvif.org/name/{camera.wyze_cam.nickname.replace(' ', '_')} onvif://www.onvif.org/location/</wsd:Scopes>
                <wsd:XAddrs>http://{bridge_ip}:{self.http_port}/onvif/device_service</wsd:XAddrs>
                <wsd:MetadataVersion>1</wsd:MetadataVersion>
            </wsd:ProbeMatch>
        </wsd:ProbeMatches>
    </soap:Body>
</soap:Envelope>'''
        
        return response.encode('utf-8')
        
    def _get_local_ip(self):
        """Get local IP address."""
        try:
            # Try environment variable first
            import os
            if 'WB_IP' in os.environ:
                return os.environ['WB_IP']
            
            # Connect to external address to find local IP
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
        except:
            return "127.0.0.1"


class OnvifHTTPHandler(BaseHTTPRequestHandler):
    """HTTP handler for ONVIF requests."""
    
    def __init__(self, *args, onvif_server=None, **kwargs):
        self.onvif_server = onvif_server
        super().__init__(*args, **kwargs)
        
    def do_GET(self):
        """Handle GET requests."""
        logger.info(f"[ONVIF] GET request to: {self.path}")
        
        if self.path == '/onvif/test':
            self._serve_test_page()
        elif self.path == '/onvif/discovery_test':
            self._serve_discovery_test()
        elif self.path == '/onvif/device_service':
            self._serve_wsdl('device')
        elif self.path == '/onvif/credential_test':
            self._serve_credential_test()
        else:
            logger.warning(f"[ONVIF] Unknown GET path: {self.path}")
            self.send_error(404)
            
    def do_POST(self):
        """Handle SOAP POST requests."""
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        
        logger.info(f"[ONVIF] POST request received, Content-Length: {content_length}")
        logger.debug(f"[ONVIF] POST data preview: {post_data.decode('utf-8')[:500]}...")
        
        try:
            # Check for WS-Security in SOAP body instead of HTTP Basic Auth
            auth_result = self._check_ws_security(post_data)
            if not auth_result:
                return  # Authentication handler already sent response
                
            response = self._handle_soap_request(post_data)
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/soap+xml; charset=utf-8')
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            
        except Exception as e:
            logger.error(f"[ONVIF] SOAP error: {e}")
            self.send_error(500)
            
    def _check_ws_security(self, soap_data: bytes):
        """Check WS-Security authentication in SOAP message."""
        try:
            soap_str = soap_data.decode('utf-8')
            
            # Check if there's a WS-Security header
            if 'Security>' in soap_str or 'UsernameToken>' in soap_str:
                logger.info("[ONVIF] WS-Security authentication detected")
                
                # Parse username from WS-Security header
                try:
                    root = etree.fromstring(soap_data)
                    username_elem = root.find('.//{http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd}Username')
                    password_elem = root.find('.//{http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd}Password')
                    
                    username = username_elem.text if username_elem is not None else "unknown"
                    password = password_elem.text if password_elem is not None else "unknown"
                    
                    logger.info(f"[ONVIF] WS-Security auth: username='{username}', password={'*' * len(password)}")
                    logger.info(f"[ONVIF] ✅ WS-Security authentication accepted")
                    return True
                    
                except Exception as parse_error:
                    logger.warning(f"[ONVIF] WS-Security parsing error: {parse_error}")
                    # Continue to accept anyway for compatibility
                    return True
            else:
                # No WS-Security found - for ONVIF compatibility, many devices allow unauthenticated access to basic operations
                logger.info("[ONVIF] No WS-Security found, allowing unauthenticated access for compatibility")
                return True
                
        except Exception as e:
            logger.error(f"[ONVIF] Authentication check error: {e}")
            # Default to allowing access for compatibility
            return True
            
    def _check_authentication(self):
        """Check HTTP Basic Authentication - accepts any credentials."""
        auth_header = self.headers.get('Authorization')
        
        if not auth_header:
            # For ONVIF compatibility, allow unauthenticated access to some operations
            logger.info("[ONVIF] No HTTP auth provided, allowing for ONVIF compatibility")
            return True
            
        # Parse Basic Auth if provided
        if auth_header.startswith('Basic '):
            try:
                import base64
                encoded_credentials = auth_header[6:]  # Remove "Basic "
                decoded_credentials = base64.b64decode(encoded_credentials).decode('utf-8')
                username, password = decoded_credentials.split(':', 1)
                
                logger.info(f"[ONVIF] HTTP Basic auth: username='{username}', password={'*' * len(password)}")
                logger.info(f"[ONVIF] ✅ HTTP Basic authentication accepted")
                return True
                
            except Exception as e:
                logger.error(f"[ONVIF] HTTP auth parsing error: {e}")
                return True  # Allow anyway for compatibility
                
        return True  # Allow all requests for now
            
    def _handle_soap_request(self, data: bytes) -> bytes:
        """Handle SOAP requests."""
        try:
            root = etree.fromstring(data)
            
            # Extract SOAP action
            body = root.find('.//{http://www.w3.org/2003/05/soap-envelope}Body')
            if body is None:
                body = root.find('.//{http://schemas.xmlsoap.org/soap/envelope/}Body')
                
            if body is not None:
                for child in body:
                    action = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                    return self._process_action(action, child)
                    
        except Exception as e:
            logger.error(f"[ONVIF] SOAP parsing error: {e}")
            
        return self._create_soap_fault("Action not supported")
        
    def _process_action(self, action: str, element) -> bytes:
        """Process ONVIF actions."""
        if action == 'GetDeviceInformation':
            return self._get_device_information()
        elif action == 'GetCapabilities':
            return self._get_capabilities()
        elif action == 'GetSystemDateAndTime':
            return self._get_system_date_time()
        elif action == 'GetProfiles':
            return self._get_profiles()
        elif action == 'GetStreamUri':
            return self._get_stream_uri(element)
        else:
            return self._create_soap_fault(f"Action {action} not implemented")
            
    def _get_device_information(self) -> bytes:
        """Get device information."""
        # Use first camera for device info
        camera = next(iter(self.onvif_server.cameras.values())) if self.onvif_server.cameras else None
        
        if not camera:
            return self._create_soap_fault("No cameras available")
            
        response = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
    <s:Body>
        <tds:GetDeviceInformationResponse>
            <tds:Manufacturer>Wyze Labs</tds:Manufacturer>
            <tds:Model>{camera.wyze_cam.product_model}</tds:Model>
            <tds:FirmwareVersion>{camera.wyze_cam.firmware_ver or "1.0.0"}</tds:FirmwareVersion>
            <tds:SerialNumber>{camera.wyze_cam.mac}</tds:SerialNumber>
            <tds:HardwareId>{camera.wyze_cam.product_model}</tds:HardwareId>
        </tds:GetDeviceInformationResponse>
    </s:Body>
</s:Envelope>'''
        
        return response.encode('utf-8')
        
    def _get_capabilities(self) -> bytes:
        """Get device capabilities."""
        bridge_ip = BRIDGE_IP or "127.0.0.1"
        
        response = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
    <s:Body>
        <tds:GetCapabilitiesResponse>
            <tds:Capabilities>
                <tt:Device>
                    <tt:XAddr>http://{bridge_ip}:{self.onvif_server.port}/onvif/device_service</tt:XAddr>
                </tt:Device>
                <tt:Media>
                    <tt:XAddr>http://{bridge_ip}:{self.onvif_server.port}/onvif/media_service</tt:XAddr>
                </tt:Media>
            </tds:Capabilities>
        </tds:GetCapabilitiesResponse>
    </s:Body>
</s:Envelope>'''
        
        return response.encode('utf-8')
        
    def _get_system_date_time(self) -> bytes:
        """Get system date and time."""
        now = datetime.now(timezone.utc)
        
        response = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
    <s:Body>
        <tds:GetSystemDateAndTimeResponse>
            <tds:SystemDateAndTime>
                <tt:DateTimeType>NTP</tt:DateTimeType>
                <tt:DaylightSavings>false</tt:DaylightSavings>
                <tt:TimeZone>
                    <tt:TZ>UTC</tt:TZ>
                </tt:TimeZone>
                <tt:UTCDateTime>
                    <tt:Time>
                        <tt:Hour>{now.hour}</tt:Hour>
                        <tt:Minute>{now.minute}</tt:Minute>
                        <tt:Second>{now.second}</tt:Second>
                    </tt:Time>
                    <tt:Date>
                        <tt:Year>{now.year}</tt:Year>
                        <tt:Month>{now.month}</tt:Month>
                        <tt:Day>{now.day}</tt:Day>
                    </tt:Date>
                </tt:UTCDateTime>
            </tds:SystemDateAndTime>
        </tds:GetSystemDateAndTimeResponse>
    </s:Body>
</s:Envelope>'''
        
        return response.encode('utf-8')
        
    def _get_profiles(self) -> bytes:
        """Get media profiles."""
        camera = next(iter(self.onvif_server.cameras.values())) if self.onvif_server.cameras else None
        
        if not camera:
            return self._create_soap_fault("No cameras available")
            
        profile = camera.profiles[0]  # Use first profile
        
        response = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
    <s:Body>
        <trt:GetProfilesResponse>
            <trt:Profiles token="{profile['token']}" fixed="true">
                <tt:Name>{profile['name']}</tt:Name>
                <tt:VideoSourceConfiguration token="{profile['video_source']['token']}">
                    <tt:Name>VideoSource</tt:Name>
                    <tt:UseCount>1</tt:UseCount>
                    <tt:SourceToken>{profile['video_source']['token']}</tt:SourceToken>
                    <tt:Bounds x="0" y="0" width="{profile['video_source']['resolution']['width']}" height="{profile['video_source']['resolution']['height']}"/>
                </tt:VideoSourceConfiguration>
                <tt:VideoEncoderConfiguration token="{profile['video_encoder']['token']}">
                    <tt:Name>VideoEncoder</tt:Name>
                    <tt:UseCount>1</tt:UseCount>
                    <tt:Encoding>{profile['video_encoder']['encoding']}</tt:Encoding>
                    <tt:Resolution>
                        <tt:Width>{profile['video_encoder']['resolution']['width']}</tt:Width>
                        <tt:Height>{profile['video_encoder']['resolution']['height']}</tt:Height>
                    </tt:Resolution>
                    <tt:Quality>50</tt:Quality>
                    <tt:RateControl>
                        <tt:FrameRateLimit>{profile['video_encoder']['framerate']}</tt:FrameRateLimit>
                        <tt:EncodingInterval>1</tt:EncodingInterval>
                        <tt:BitrateLimit>{profile['video_encoder']['bitrate']}</tt:BitrateLimit>
                    </tt:RateControl>
                    <tt:H264>
                        <tt:GovLength>30</tt:GovLength>
                        <tt:H264Profile>Baseline</tt:H264Profile>
                    </tt:H264>
                </tt:VideoEncoderConfiguration>
            </trt:Profiles>
        </trt:GetProfilesResponse>
    </s:Body>
</s:Envelope>'''
        
        return response.encode('utf-8')
        
    def _get_stream_uri(self, element) -> bytes:
        """Get stream URI."""
        camera = next(iter(self.onvif_server.cameras.values())) if self.onvif_server.cameras else None
        
        if not camera:
            return self._create_soap_fault("No cameras available")
            
        bridge_ip = BRIDGE_IP or "127.0.0.1"
        from wyzebridge.config import STREAM_AUTH
        auth_prefix = ""
        if STREAM_AUTH and ":" in str(STREAM_AUTH):
            first_creds = str(STREAM_AUTH).split("|")[0].split("@")[0]
            parts = first_creds.split(":", 2)
            if len(parts) >= 2:
                auth_prefix = f"{parts[0]}:{parts[1]}@"
        rtsp_url = f"rtsp://{auth_prefix}{bridge_ip}:8554/{camera.stream_uri}"
        
        response = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
    <s:Body>
        <trt:GetStreamUriResponse>
            <trt:MediaUri>
                <tt:Uri>{rtsp_url}</tt:Uri>
                <tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>
                <tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>
                <tt:Timeout>PT0S</tt:Timeout>
            </trt:MediaUri>
        </trt:GetStreamUriResponse>
    </s:Body>
</s:Envelope>'''
        
        return response.encode('utf-8')
        
    def _create_soap_fault(self, message: str) -> bytes:
        """Create SOAP fault response."""
        response = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
    <s:Body>
        <s:Fault>
            <s:Code>
                <s:Value>s:Receiver</s:Value>
            </s:Code>
            <s:Reason>
                <s:Text xml:lang="en">{message}</s:Text>
            </s:Reason>
        </s:Fault>
    </s:Body>
</s:Envelope>'''
        
        return response.encode('utf-8')
        
    def _serve_test_page(self):
        """Serve ONVIF test page."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        
        bridge_ip = BRIDGE_IP or "127.0.0.1"
        camera_count = len(self.onvif_server.cameras) if self.onvif_server else 0
        
        html = f'''<!DOCTYPE html>
<html>
<head><title>ONVIF Server Status</title></head>
<body>
    <h1>Wyze Bridge ONVIF Server Status</h1>
    <h2>Configuration</h2>
    <ul>
        <li>Bridge IP: {bridge_ip}</li>
        <li>HTTP Port: {self.onvif_server.port if self.onvif_server else 'Unknown'}</li>
        <li>Camera Count: {camera_count}</li>
    </ul>
    
    <h2>Cameras</h2>
    <ul>
    '''
        
        if self.onvif_server and self.onvif_server.cameras:
            for uri, camera in self.onvif_server.cameras.items():
                html += f'<li>{camera.wyze_cam.nickname} ({uri}) - UUID: {camera.uuid}</li>'
        else:
            html += '<li>No cameras found</li>'
            
        html += f'''
    </ul>
    
    <h2>Services</h2>
    <ul>
        <li><a href="/onvif/device_service">Device Service WSDL</a></li>
        <li><a href="/onvif/discovery_test">Discovery Test</a></li>
    </ul>
    
    <h2>Manual Discovery Test</h2>
    <p>To test discovery manually, send a UDP packet to port 3702 with WS-Discovery Probe message.</p>
    <p>Expected response should contain ProbeMatch with XAddrs pointing to http://{bridge_ip}:{self.onvif_server.port if self.onvif_server else 8080}/onvif/device_service</p>
</body>
</html>'''
        
        self.wfile.write(html.encode('utf-8'))
        
    def _serve_discovery_test(self):
        """Serve discovery test endpoint."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        
        result = "ONVIF Discovery Test\n"
        result += "===================\n\n"
        
        if not self.onvif_server or not self.onvif_server.cameras:
            result += "❌ ERROR: No cameras available for ONVIF\n"
            result += "Check that:\n"
            result += "- Cameras are connected and authenticated\n"
            result += "- ONVIF_ENABLE=true in environment\n"
            result += "- Container has proper network access\n"
        else:
            result += f"✅ {len(self.onvif_server.cameras)} cameras available\n\n"
            
            for uri, camera in self.onvif_server.cameras.items():
                result += f"Camera: {camera.wyze_cam.nickname}\n"
                result += f"  URI: {uri}\n"
                result += f"  UUID: {camera.uuid}\n"
                result += f"  MAC: {camera.wyze_cam.mac}\n"
                result += f"  Model: {camera.wyze_cam.product_model}\n\n"
        
        # Network info
        try:
            import socket
            hostname = socket.gethostname()
            local_ip = socket.gethostbyname(hostname)
            result += f"Host Info:\n"
            result += f"  Hostname: {hostname}\n"
            result += f"  Local IP: {local_ip}\n"
            result += f"  Bridge IP: {BRIDGE_IP or 'Not set'}\n\n"
        except:
            result += "Could not get network info\n\n"
            
        # Discovery server status
        if self.onvif_server and self.onvif_server.discovery_server:
            result += f"Discovery Server: {'Running' if self.onvif_server.discovery_server.running else 'Stopped'}\n"
        else:
            result += "Discovery Server: Not started\n"
            
        result += "\nTo test discovery:\n"
        result += "1. Ensure UniFi controller is on same network\n"
        result += "2. Check Docker network mode (should be bridge or host)\n"
        result += "3. Verify ports 8080 and 3702/udp are accessible\n"
        result += "4. Check container logs for '[ONVIF]' messages\n"
        
        self.wfile.write(result.encode('utf-8'))
        
    def _serve_credential_test(self):
        """Test credential validation like UniFi would."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        
        bridge_ip = BRIDGE_IP or "127.0.0.1"
        
        html = f'''<!DOCTYPE html>
<html>
<head>
    <title>ONVIF Credential Test</title>
    <script>
        async function testCredentials() {{
            const username = document.getElementById('username').value || 'admin';
            const password = document.getElementById('password').value || 'password';
            const result = document.getElementById('result');
            
            // Test GetCapabilities request like UniFi does
            const soapRequest = `<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
    <soap:Header>
        <Security xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
            <UsernameToken>
                <Username>${{username}}</Username>
                <Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText">${{password}}</Password>
            </UsernameToken>
        </Security>
    </soap:Header>
    <soap:Body>
        <tds:GetCapabilities/>
    </soap:Body>
</soap:Envelope>`;
            
            try {{
                const response = await fetch('http://{bridge_ip}:8080/onvif/device_service', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/soap+xml; charset=utf-8',
                        'SOAPAction': 'http://www.onvif.org/ver10/device/wsdl/GetCapabilities'
                    }},
                    body: soapRequest
                }});
                
                const responseText = await response.text();
                result.innerHTML = `<h3>Response Status: ${{response.status}}</h3>
                                   <h4>Response Body:</h4>
                                   <pre>${{responseText}}</pre>`;
                                   
            }} catch (error) {{
                result.innerHTML = `<h3>Error:</h3><pre>${{error.message}}</pre>`;
            }}
        }}
    </script>
</head>
<body>
    <h1>ONVIF Credential Test</h1>
    <p>This simulates what UniFi does when testing credentials.</p>
    
    <div>
        <label>Username: <input type="text" id="username" value="admin" placeholder="admin"/></label><br><br>
        <label>Password: <input type="password" id="password" value="password" placeholder="password"/></label><br><br>
        <button onclick="testCredentials()">Test Credentials</button>
    </div>
    
    <div id="result" style="margin-top: 20px; border: 1px solid #ccc; padding: 10px; background: #f9f9f9;">
        Click "Test Credentials" to see what happens...
    </div>
</body>
</html>'''
        
        self.wfile.write(html.encode('utf-8'))
        
    def _serve_wsdl(self, service_type: str):
        """Serve WSDL for service discovery."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/xml')
        self.end_headers()
        
        # Minimal WSDL for device service
        wsdl = '''<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://schemas.xmlsoap.org/wsdl/" xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
    <service name="DeviceService">
        <port name="DevicePort" binding="tds:DeviceBinding">
            <soap:address location="http://localhost:8080/onvif/device_service"/>
        </port>
    </service>
</definitions>'''
        
        self.wfile.write(wsdl.encode('utf-8'))
        
    def log_message(self, format, *args):
        """Override to use custom logger."""
        logger.debug(f"[ONVIF] HTTP {format % args}")


class OnvifServer:
    """Main ONVIF server controller."""
    
    def __init__(self, streams: Dict, port: int = 8080):
        self.streams = streams
        self.port = port
        self.cameras: Dict[str, OnvifCamera] = {}
        self.discovery_server = None
        self.http_server = None
        self.http_thread = None
        
    def setup(self):
        """Setup ONVIF cameras and services."""
        logger.info(f"[ONVIF] ====== ONVIF SETUP DEBUG ======")
        logger.info(f"[ONVIF] Setting up ONVIF server with {len(self.streams)} streams")
        logger.info(f"[ONVIF] Stream types: {[type(stream).__name__ for stream in self.streams.values()]}")
        
        # Debug stream contents
        for uri, stream in self.streams.items():
            logger.info(f"[ONVIF] Stream {uri}:")
            logger.info(f"[ONVIF]   - Type: {type(stream).__name__}")
            logger.info(f"[ONVIF]   - Has camera attr: {hasattr(stream, 'camera')}")
            if hasattr(stream, 'camera'):
                camera = stream.camera
                logger.info(f"[ONVIF]   - Camera: {camera.nickname} ({camera.mac})")
                logger.info(f"[ONVIF]   - Camera type: {type(camera).__name__}")
                logger.info(f"[ONVIF]   - Product model: {camera.product_model}")
                self.cameras[uri] = OnvifCamera(camera, uri, stream)
            else:
                # Try to find camera in other attributes
                attrs = [attr for attr in dir(stream) if not attr.startswith('_')]
                logger.warning(f"[ONVIF]   - Available attributes: {attrs}")
                
        logger.info(f"[ONVIF] Final camera count: {len(self.cameras)}")
        if self.cameras:
            for uri, cam in self.cameras.items():
                logger.info(f"[ONVIF] Camera {uri}: {cam.wyze_cam.nickname} - UUID: {cam.uuid}")
        else:
            logger.error("[ONVIF] NO CAMERAS FOUND! ONVIF will not work.")
            logger.info("[ONVIF] This could be due to:")
            logger.info("[ONVIF]   1. Cameras not yet initialized when ONVIF setup runs")
            logger.info("[ONVIF]   2. Stream objects don't have 'camera' attribute")
            logger.info("[ONVIF]   3. Camera authentication issues")
        
        logger.info(f"[ONVIF] ====== END ONVIF SETUP ======")
        
    def start(self):
        """Start ONVIF services."""
        logger.info("[ONVIF] ====== STARTING ONVIF SERVICES ======")
        
        if not self.cameras:
            logger.error("[ONVIF] ❌ CRITICAL: No cameras to expose via ONVIF!")
            logger.error("[ONVIF] ONVIF services will not start. Check camera initialization.")
            return False
            
        # Start HTTP server
        logger.info("[ONVIF] Starting HTTP server...")
        self._start_http_server()
        
        # Give HTTP server time to start
        import time
        time.sleep(0.5)
        
        # Start WS-Discovery server
        logger.info("[ONVIF] Starting WS-Discovery server...")
        self.discovery_server = WSDiscoveryServer(self.cameras, self.port)
        self.discovery_server.start()
        
        # Give discovery server time to start
        time.sleep(0.5)
        
        logger.info(f"[ONVIF] ✅ ONVIF server fully started!")
        logger.info(f"[ONVIF]   - HTTP server: 0.0.0.0:{self.port}")
        logger.info(f"[ONVIF]   - WS-Discovery: 0.0.0.0:3702 (multicast 239.255.255.250)")
        logger.info(f"[ONVIF]   - Cameras available: {len(self.cameras)}")
        
        # Log camera details
        for uri, camera in self.cameras.items():
            logger.info(f"[ONVIF]     * {camera.wyze_cam.nickname} ({uri}) - UUID: {camera.uuid}")
            
        logger.info("[ONVIF] UniFi should now be able to discover these cameras!")
        return True
        
    def _start_http_server(self):
        """Start HTTP server for ONVIF services."""
        def handler(*args, **kwargs):
            return OnvifHTTPHandler(*args, onvif_server=self, **kwargs)
            
        def run_server():
            try:
                self.http_server = HTTPServer(('0.0.0.0', self.port), handler)
                logger.info(f"[ONVIF] HTTP server listening on 0.0.0.0:{self.port}")
                self.http_server.serve_forever()
            except Exception as e:
                logger.error(f"[ONVIF] HTTP server error: {e}")
                
        self.http_thread = threading.Thread(target=run_server, daemon=True)
        self.http_thread.start()
        
    def stop(self):
        """Stop ONVIF services."""
        if self.discovery_server:
            self.discovery_server.stop()
            
        if self.http_server:
            self.http_server.shutdown()
            
        logger.info("[ONVIF] Server stopped")