import logging
import ipaddress
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
import secrets
import hashlib
import hmac
import jwt

from fastapi import Request, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Security configuration
security = HTTPBearer(auto_error=False)


class AuthenticationError(HTTPException):
    """Custom authentication error"""
    
    def __init__(self, detail: str = "Authentication failed"):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"}
        )


class AuthorizationError(HTTPException):
    """Custom authorization error"""
    
    def __init__(self, detail: str = "Insufficient permissions"):
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=detail
        )


def get_client_ip(request: Request) -> str:
    """
    Extract client IP address from request headers.
    Handles X-Forwarded-For, X-Real-IP headers and direct connection.
    """
    # Check X-Forwarded-For header (proxy/load balancer)
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # Take the first IP in the chain (original client)
        client_ip = forwarded_for.split(",")[0].strip()
        return client_ip
    
    # Check X-Real-IP header (nginx proxy)
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    
    # Check CF-Connecting-IP (Cloudflare)
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    
    # Fallback to direct connection
    client_host = request.client.host if request.client else "unknown"
    return client_host


def get_client_ip_from_websocket(websocket) -> str:
    """Extract client IP from WebSocket connection"""
    # Check headers similar to HTTP requests
    headers = dict(websocket.headers)
    
    # Check X-Forwarded-For
    forwarded_for = headers.get(b"x-forwarded-for")
    if forwarded_for:
        client_ip = forwarded_for.decode().split(",")[0].strip()
        return client_ip
    
    # Check X-Real-IP
    real_ip = headers.get(b"x-real-ip")
    if real_ip:
        return real_ip.decode().strip()
    
    # Check CF-Connecting-IP
    cf_ip = headers.get(b"cf-connecting-ip")
    if cf_ip:
        return cf_ip.decode().strip()
    
    # Fallback to client host
    client_host = websocket.client.host if websocket.client else "unknown"
    return client_host


def is_private_ip(ip_address: str) -> bool:
    """Check if IP address is private/internal"""
    try:
        ip = ipaddress.ip_address(ip_address)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


def is_localhost(ip_address: str) -> bool:
    """Check if IP address is localhost"""
    try:
        ip = ipaddress.ip_address(ip_address)
        return ip.is_loopback
    except ValueError:
        return ip_address.lower() in ("localhost", "local")


def validate_ip_access(ip_address: str, allowed_ips: List[str] = None, 
                      allow_private: bool = True, allow_localhost: bool = True) -> bool:
    """
    Validate if IP address is allowed access.
    
    Args:
        ip_address: Client IP address
        allowed_ips: List of specifically allowed IPs/CIDR ranges
        allow_private: Whether to allow private IPs
        allow_localhost: Whether to allow localhost
    """
    try:
        ip = ipaddress.ip_address(ip_address)
        
        # Check against allowed IPs list
        if allowed_ips:
            for allowed_ip in allowed_ips:
                try:
                    if "/" in allowed_ip:
                        # CIDR range
                        if ip in ipaddress.ip_network(allowed_ip, strict=False):
                            return True
                    else:
                        # Single IP
                        if ip == ipaddress.ip_address(allowed_ip):
                            return True
                except ValueError:
                    continue
            # If we have an allowed IPs list and IP doesn't match, deny
            return False
        
        # Check localhost
        if ip.is_loopback:
            return allow_localhost
        
        # Check private IPs
        if ip.is_private:
            return allow_private
        
        # Public IP - allow by default if no restrictions
        return True
        
    except ValueError:
        # Invalid IP format
        return False


def generate_session_token(session_id: str, secret_key: str) -> str:
    """Generate a secure session token"""
    # Create payload
    payload = {
        'session_id': session_id,
        'issued_at': datetime.utcnow().timestamp(),
        'expires_at': (datetime.utcnow() + timedelta(hours=24)).timestamp()
    }
    
    # Create JWT token
    token = jwt.encode(payload, secret_key, algorithm='HS256')
    return token


def verify_session_token(token: str, secret_key: str) -> Optional[Dict[str, Any]]:
    """Verify and decode session token"""
    try:
        payload = jwt.decode(token, secret_key, algorithms=['HS256'])
        
        # Check expiration
        if payload.get('expires_at', 0) < datetime.utcnow().timestamp():
            return None
        
        return payload
    except jwt.InvalidTokenError:
        return None


def create_api_key(user_id: str, permissions: List[str] = None) -> str:
    """Create an API key for programmatic access"""
    # Generate random key
    random_part = secrets.token_urlsafe(32)
    
    # Create prefix with metadata
    prefix = f"bua_{user_id[:8]}"  # bua = Browser Use Automation
    
    # Combine and return
    api_key = f"{prefix}_{random_part}"
    return api_key


def verify_api_key(api_key: str, valid_keys: Dict[str, Dict]) -> Optional[Dict[str, Any]]:
    """Verify API key and return associated metadata"""
    if not api_key or not api_key.startswith("bua_"):
        return None
    
    return valid_keys.get(api_key)


def hash_password(password: str, salt: str = None) -> tuple[str, str]:
    """Hash password with salt"""
    if salt is None:
        salt = secrets.token_hex(16)
    
    # Use PBKDF2 with SHA256
    password_hash = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt.encode('utf-8'),
        100000  # iterations
    )
    
    return password_hash.hex(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    """Verify password against hash"""
    computed_hash, _ = hash_password(password, salt)
    return hmac.compare_digest(computed_hash, password_hash)


async def validate_session_access(session_id: str, request: Request) -> Any:
    """
    Validate that the request has access to the specified session.
    Returns the session object if valid.
    """
    from src.core.session_manager import get_session_manager
    
    try:
        session_manager = get_session_manager()
        session = await session_manager.get_session(session_id)
        
        if not session:
            raise HTTPException(
                status_code=404,
                detail="Session not found"
            )
        
        # Check IP-based access (basic security)
        client_ip = get_client_ip(request)
        
        # Allow access if:
        # 1. Session has no IP restriction, OR
        # 2. Client IP matches session IP, OR  
        # 3. Client IP is localhost/private (for development)
        if (session.client_ip and 
            session.client_ip != client_ip and 
            not is_localhost(client_ip) and 
            not is_private_ip(client_ip)):
            
            logger.warning(f"IP mismatch for session {session_id}: {client_ip} vs {session.client_ip}")
            raise HTTPException(
                status_code=403,
                detail="Access denied: IP address mismatch"
            )
        
        return session
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error validating session access: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Session validation failed"
        )


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> Optional[Dict[str, Any]]:
    """
    Get current user from Bearer token.
    Returns None if no valid token provided (for optional auth).
    """
    if not credentials:
        return None
    
    from src.config.settings import get_settings
    settings = get_settings()
    
    try:
        # Try JWT token first
        payload = verify_session_token(credentials.credentials, settings.secret_key)
        if payload:
            return {
                'type': 'session',
                'session_id': payload.get('session_id'),
                'authenticated': True
            }
        
        # Try API key
        # In a real implementation, you'd load this from a database
        valid_api_keys = {
            # Example API key - in production, store these securely
            "bua_admin123_example": {
                'user_id': 'admin',
                'permissions': ['admin', 'read', 'write'],
                'created_at': '2024-01-01T00:00:00Z'
            }
        }
        
        api_key_info = verify_api_key(credentials.credentials, valid_api_keys)
        if api_key_info:
            return {
                'type': 'api_key',
                'user_id': api_key_info['user_id'],
                'permissions': api_key_info['permissions'],
                'authenticated': True
            }
        
        return None
        
    except Exception as e:
        logger.debug(f"Token validation failed: {e}")
        return None


async def require_authentication(current_user: Optional[Dict] = Depends(get_current_user)) -> Dict[str, Any]:
    """Require authentication - raises 401 if not authenticated"""
    if not current_user or not current_user.get('authenticated'):
        raise AuthenticationError("Authentication required")
    
    return current_user


async def require_permission(permission: str, current_user: Dict = Depends(require_authentication)) -> Dict[str, Any]:
    """Require specific permission"""
    user_permissions = current_user.get('permissions', [])
    
    if 'admin' in user_permissions or permission in user_permissions:
        return current_user
    
    raise AuthorizationError(f"Permission '{permission}' required")


async def require_admin_access(current_user: Dict = Depends(require_authentication)) -> bool:
    """Require admin access - for admin endpoints"""
    user_permissions = current_user.get('permissions', [])
    
    if 'admin' in user_permissions:
        return True
    
    # Also check if user is accessing from admin IP
    # This would need request context, but for simplicity we'll just check permissions
    raise AuthorizationError("Admin access required")


def check_rate_limit_exemption(request: Request) -> bool:
    """Check if request should be exempt from rate limiting"""
    client_ip = get_client_ip(request)
    
    # Exempt localhost and private IPs from rate limiting in development
    from src.config.settings import get_settings
    settings = get_settings()
    
    if settings.is_development():
        if is_localhost(client_ip) or is_private_ip(client_ip):
            return True
    
    # Check for admin IPs
    if client_ip in settings.admin_ips:
        return True
    
    # Check for API key with rate limit exemption
    auth_header = request.headers.get('Authorization')
    if auth_header and auth_header.startswith('Bearer '):
        token = auth_header[7:]
        # In production, you'd check if this API key has rate limit exemption
        if token.startswith('bua_admin'):
            return True
    
    return False


def create_security_headers() -> Dict[str, str]:
    """Create security headers for responses"""
    return {
        'X-Content-Type-Options': 'nosniff',
        'X-Frame-Options': 'DENY',
        'X-XSS-Protection': '1; mode=block',
        'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
        'Referrer-Policy': 'strict-origin-when-cross-origin'
    }


class SecurityMiddleware:
    """Middleware for adding security headers and basic security checks"""
    
    def __init__(self, app):
        self.app = app
    
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            # Add security headers to response
            async def send_wrapper(message):
                if message["type"] == "http.response.start":
                    headers = dict(message.get("headers", []))
                    
                    # Add security headers
                    security_headers = create_security_headers()
                    for key, value in security_headers.items():
                        headers[key.encode()] = value.encode()
                    
                    message["headers"] = list(headers.items())
                
                await send(message)
            
            await self.app(scope, receive, send_wrapper)
        else:
            await self.app(scope, receive, send)


class IPWhitelistMiddleware:
    """Middleware for IP-based access control"""
    
    def __init__(self, app, allowed_ips: List[str] = None, 
                 allow_private: bool = True, allow_localhost: bool = True):
        self.app = app
        self.allowed_ips = allowed_ips or []
        self.allow_private = allow_private
        self.allow_localhost = allow_localhost
    
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            # Create a dummy request to get IP
            request = Request(scope, receive)
            client_ip = get_client_ip(request)
            
            # Validate IP access
            if not validate_ip_access(
                client_ip, 
                self.allowed_ips, 
                self.allow_private, 
                self.allow_localhost
            ):
                # Send 403 Forbidden
                response = JSONResponse(
                    status_code=403,
                    content={"detail": "Access denied from this IP address"}
                )
                await response(scope, receive, send)
                return
        
        await self.app(scope, receive, send)


def get_user_agent(request: Request) -> str:
    """Get User-Agent header from request"""
    return request.headers.get("User-Agent", "unknown")


def get_request_fingerprint(request: Request) -> str:
    """Create a fingerprint for the request"""
    client_ip = get_client_ip(request)
    user_agent = get_user_agent(request)
    
    # Create a simple fingerprint
    fingerprint_data = f"{client_ip}:{user_agent}"
    fingerprint = hashlib.sha256(fingerprint_data.encode()).hexdigest()[:16]
    
    return fingerprint


def log_security_event(event_type: str, details: Dict[str, Any], request: Request = None):
    """Log security-related events"""
    log_data = {
        'event_type': event_type,
        'timestamp': datetime.utcnow().isoformat(),
        'details': details
    }
    
    if request:
        log_data.update({
            'client_ip': get_client_ip(request),
            'user_agent': get_user_agent(request),
            'path': request.url.path,
            'method': request.method
        })
    
    logger.warning(f"SECURITY EVENT: {event_type} - {log_data}")


# Utility functions for common auth patterns

def session_required(func):
    """Decorator to require valid session"""
    async def wrapper(*args, **kwargs):
        # This would be used with FastAPI dependencies
        # The actual implementation would depend on how you structure your endpoints
        return await func(*args, **kwargs)
    return wrapper


def admin_required(func):
    """Decorator to require admin privileges"""
    async def wrapper(*args, **kwargs):
        # Similar to session_required but for admin access
        return await func(*args, **kwargs)
    return wrapper


# Export main components
__all__ = [
    'AuthenticationError',
    'AuthorizationError',
    'get_client_ip',
    'get_client_ip_from_websocket',
    'is_private_ip',
    'is_localhost',
    'validate_ip_access',
    'generate_session_token',
    'verify_session_token',
    'create_api_key',
    'verify_api_key',
    'hash_password',
    'verify_password',
    'validate_session_access',
    'get_current_user',
    'require_authentication',
    'require_permission',
    'require_admin_access',
    'check_rate_limit_exemption',
    'create_security_headers',
    'SecurityMiddleware',
    'IPWhitelistMiddleware',
    'get_user_agent',
    'get_request_fingerprint',
    'log_security_event'
]