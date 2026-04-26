"""
ONVIF PTZ Service implementation for Wyze Pan cameras.
Maps ONVIF PTZ commands to Wyze camera controls.
"""

import asyncio
from typing import Dict, Optional, Tuple

from spyne import ServiceBase, rpc, String, Float, Integer, Boolean
from spyne.model.complex import ComplexModel

from wyzebridge.logging import logger
from wyzebridge.wyze_commands import GET_CMDS, SET_CMDS
from wyzecam.tutk import tutk_protocol


class PTZService(ServiceBase):
    """ONVIF PTZ Service for camera control."""
    
    @rpc(String, _returns=ComplexModel)
    def GetNodes(ctx, ProfileToken):
        """Get PTZ nodes."""
        camera = ctx.transport.req_env.get('camera')
        if not camera or not camera.wyze_cam.is_pan_cam:
            return
            
        return {
            'PTZNode': [{
                'token': f'ptz_{camera.wyze_cam.name_uri}',
                'Name': f'{camera.wyze_cam.nickname} PTZ',
                'SupportedPTZSpaces': {
                    'AbsolutePanTiltPositionSpace': [{
                        'URI': 'http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace',
                        'XRange': {'Min': -180.0, 'Max': 180.0},
                        'YRange': {'Min': -90.0, 'Max': 90.0},
                    }],
                    'RelativePanTiltTranslationSpace': [{
                        'URI': 'http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationGenericSpace',
                        'XRange': {'Min': -1.0, 'Max': 1.0},
                        'YRange': {'Min': -1.0, 'Max': 1.0},
                    }],
                    'ContinuousPanTiltVelocitySpace': [{
                        'URI': 'http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace',
                        'XRange': {'Min': -1.0, 'Max': 1.0},
                        'YRange': {'Min': -1.0, 'Max': 1.0},
                    }],
                    'PanTiltSpeedSpace': [{
                        'URI': 'http://www.onvif.org/ver10/tptz/PanTiltSpaces/GenericSpeedSpace',
                        'XRange': {'Min': 0.0, 'Max': 1.0},
                    }],
                },
                'MaximumNumberOfPresets': 4,
                'HomeSupported': True,
            }]
        }
        
    @rpc(String, _returns=ComplexModel)
    def GetConfiguration(ctx, PTZConfigurationToken):
        """Get PTZ configuration."""
        camera = ctx.transport.req_env.get('camera')
        if not camera or not camera.wyze_cam.is_pan_cam:
            return
            
        return {
            'PTZConfiguration': {
                'token': f'ptz_config_{camera.wyze_cam.name_uri}',
                'Name': f'{camera.wyze_cam.nickname} PTZ Config',
                'UseCount': 1,
                'NodeToken': f'ptz_{camera.wyze_cam.name_uri}',
                'DefaultAbsolutePantTiltPositionSpace': 'http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace',
                'DefaultRelativePanTiltTranslationSpace': 'http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationGenericSpace',
                'DefaultContinuousPanTiltVelocitySpace': 'http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace',
                'DefaultPTZSpeed': {
                    'PanTilt': {'x': 0.5, 'y': 0.5},
                },
                'PanTiltLimits': {
                    'Range': {
                        'URI': 'http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace',
                        'XRange': {'Min': -180.0, 'Max': 180.0},
                        'YRange': {'Min': -90.0, 'Max': 90.0},
                    }
                },
            }
        }
        
    @rpc(String, _returns=ComplexModel)
    def GetStatus(ctx, ProfileToken):
        """Get current PTZ status."""
        camera = ctx.transport.req_env.get('camera')
        stream = ctx.transport.req_env.get('stream')
        
        if not camera or not stream or not camera.wyze_cam.is_pan_cam:
            return
            
        # Get current position from camera
        try:
            position = stream.send_cmd('ptz_position')
            if position and 'value' in position:
                pan = position['value'].get('horizontal', 0)
                tilt = position['value'].get('vertical', 0)
            else:
                pan = tilt = 0
        except:
            pan = tilt = 0
            
        return {
            'PTZStatus': {
                'Position': {
                    'PanTilt': {
                        'x': float(pan),
                        'y': float(tilt),
                        'space': 'http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace',
                    }
                },
                'MoveStatus': {
                    'PanTilt': 'IDLE',
                },
            }
        }
        
    @rpc(String, ComplexModel, ComplexModel, _returns=Boolean)
    def AbsoluteMove(ctx, ProfileToken, Position, Speed):
        """Move to absolute position."""
        camera = ctx.transport.req_env.get('camera')
        stream = ctx.transport.req_env.get('stream')
        
        if not camera or not stream or not camera.wyze_cam.is_pan_cam:
            return False
            
        try:
            # Extract pan/tilt values
            pan = Position.get('PanTilt', {}).get('x', 0)
            tilt = Position.get('PanTilt', {}).get('y', 0)
            
            # Convert ONVIF coordinates to Wyze coordinates
            # ONVIF: Pan -180 to 180, Tilt -90 to 90
            # Wyze: Pan 0 to 360, Tilt -90 to 90
            wyze_pan = int((pan + 180) % 360)
            wyze_tilt = int(tilt)
            
            # Send PTZ command
            result = stream.send_cmd('ptz_position', [wyze_pan, wyze_tilt])
            
            return result.get('status') == 'success'
        except Exception as e:
            logger.error(f"[ONVIF] PTZ AbsoluteMove error: {e}")
            return False
            
    @rpc(String, ComplexModel, ComplexModel, _returns=Boolean)
    def RelativeMove(ctx, ProfileToken, Translation, Speed):
        """Move relative to current position."""
        camera = ctx.transport.req_env.get('camera')
        stream = ctx.transport.req_env.get('stream')
        
        if not camera or not stream or not camera.wyze_cam.is_pan_cam:
            return False
            
        try:
            # Get current position
            current = stream.send_cmd('ptz_position')
            if not current or 'value' not in current:
                return False
                
            current_pan = current['value'].get('horizontal', 0)
            current_tilt = current['value'].get('vertical', 0)
            
            # Extract relative movement
            rel_pan = Translation.get('PanTilt', {}).get('x', 0)
            rel_tilt = Translation.get('PanTilt', {}).get('y', 0)
            
            # Calculate new position
            new_pan = int((current_pan + rel_pan * 45) % 360)  # 45 degrees per unit
            new_tilt = int(max(-90, min(90, current_tilt + rel_tilt * 30)))  # 30 degrees per unit
            
            # Send PTZ command
            result = stream.send_cmd('ptz_position', [new_pan, new_tilt])
            
            return result.get('status') == 'success'
        except Exception as e:
            logger.error(f"[ONVIF] PTZ RelativeMove error: {e}")
            return False
            
    @rpc(String, ComplexModel, Integer, _returns=Boolean)
    def ContinuousMove(ctx, ProfileToken, Velocity, Timeout):
        """Continuous movement with velocity."""
        camera = ctx.transport.req_env.get('camera')
        stream = ctx.transport.req_env.get('stream')
        
        if not camera or not stream or not camera.wyze_cam.is_pan_cam:
            return False
            
        try:
            # Extract velocity
            pan_vel = Velocity.get('PanTilt', {}).get('x', 0)
            tilt_vel = Velocity.get('PanTilt', {}).get('y', 0)
            
            # Map velocity to Wyze rotary commands
            if abs(pan_vel) > 0.1:
                direction = 'right' if pan_vel > 0 else 'left'
                stream.send_cmd('rotary_degree', direction)
            elif abs(tilt_vel) > 0.1:
                direction = 'up' if tilt_vel > 0 else 'down'
                stream.send_cmd('rotary_degree', direction)
                
            return True
        except Exception as e:
            logger.error(f"[ONVIF] PTZ ContinuousMove error: {e}")
            return False
            
    @rpc(String, Boolean, _returns=Boolean)
    def Stop(ctx, ProfileToken, PanTilt):
        """Stop PTZ movement."""
        # Wyze cameras stop automatically after rotary commands
        return True
        
    @rpc(String, _returns=ComplexModel)
    def GetPresets(ctx, ProfileToken):
        """Get camera presets (cruise points)."""
        camera = ctx.transport.req_env.get('camera')
        stream = ctx.transport.req_env.get('stream')
        
        if not camera or not stream or not camera.wyze_cam.is_pan_cam:
            return
            
        try:
            # Get cruise points from camera
            points = stream.send_cmd('cruise_points')
            presets = []
            
            if points and 'value' in points:
                for i, point in enumerate(points['value'], 1):
                    if point:  # Check if point is set
                        presets.append({
                            'token': str(i),
                            'Name': f'Preset {i}',
                            'PTZPosition': {
                                'PanTilt': {
                                    'x': float(point.get('horizontal', 0)),
                                    'y': float(point.get('vertical', 0)),
                                }
                            }
                        })
                        
            return {'Preset': presets}
        except Exception as e:
            logger.error(f"[ONVIF] PTZ GetPresets error: {e}")
            return {'Preset': []}
            
    @rpc(String, String, String, ComplexModel, _returns=String)
    def SetPreset(ctx, ProfileToken, PresetName, PresetToken, Position):
        """Set a preset position."""
        camera = ctx.transport.req_env.get('camera')
        stream = ctx.transport.req_env.get('stream')
        
        if not camera or not stream or not camera.wyze_cam.is_pan_cam:
            return None
            
        try:
            # Get current position if not provided
            if not Position:
                current = stream.send_cmd('ptz_position')
                if current and 'value' in current:
                    pan = current['value'].get('horizontal', 0)
                    tilt = current['value'].get('vertical', 0)
                else:
                    return None
            else:
                pan = Position.get('PanTilt', {}).get('x', 0)
                tilt = Position.get('PanTilt', {}).get('y', 0)
                
            # Set cruise point (using next available slot)
            token = PresetToken or '1'
            
            # Note: Wyze API may have specific requirements for setting cruise points
            # This is a simplified implementation
            result = stream.send_cmd('cruise_points', {'point': int(token), 'pan': pan, 'tilt': tilt})
            
            if result.get('status') == 'success':
                return token
            return None
        except Exception as e:
            logger.error(f"[ONVIF] PTZ SetPreset error: {e}")
            return None
            
    @rpc(String, String, ComplexModel, _returns=Boolean)
    def GotoPreset(ctx, ProfileToken, PresetToken, Speed):
        """Go to a preset position."""
        camera = ctx.transport.req_env.get('camera')
        stream = ctx.transport.req_env.get('stream')
        
        if not camera or not stream or not camera.wyze_cam.is_pan_cam:
            return False
            
        try:
            # Go to cruise point
            result = stream.send_cmd('cruise_point', int(PresetToken))
            return result.get('status') == 'success'
        except Exception as e:
            logger.error(f"[ONVIF] PTZ GotoPreset error: {e}")
            return False
            
    @rpc(String, _returns=Boolean)
    def GotoHomePosition(ctx, ProfileToken):
        """Go to home position."""
        camera = ctx.transport.req_env.get('camera')
        stream = ctx.transport.req_env.get('stream')
        
        if not camera or not stream or not camera.wyze_cam.is_pan_cam:
            return False
            
        try:
            # Reset to center position (home)
            result = stream.send_cmd('reset_rotation')
            return result.get('status') == 'success'
        except Exception as e:
            logger.error(f"[ONVIF] PTZ GotoHome error: {e}")
            return False