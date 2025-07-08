import asyncio
import time
import logging
from typing import Dict, List, Optional, Tuple, Any
from collections import defaultdict, deque
from datetime import datetime, timedelta
import weakref
import json

from fastapi import Request, Response, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


class RateLimitExceeded(HTTPException):
    """Custom exception for rate limit exceeded"""
    
    def __init__(self, retry_after: int = None, detail: str = "Rate limit exceeded"):
        self.retry_after = retry_after
        super().__init__(status_code=429, detail=detail)


class TokenBucket:
    """Token bucket algorithm implementation for rate limiting"""
    
    def __init__(self, capacity: int, refill_rate: float, refill_period: float = 1.0):
        """
        Initialize token bucket.
        
        Args:
            capacity: Maximum number of tokens
            refill_rate: Number of tokens added per refill_period
            refill_period: Time period for refill in seconds
        """
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.refill_period = refill_period
        self.tokens = capacity
        self.last_refill = time.time()
        self._lock = asyncio.Lock()
    
    async def consume(self, tokens: int = 1) -> bool:
        """
        Try to consume tokens from bucket.
        
        Args:
            tokens: Number of tokens to consume
            
        Returns:
            bool: True if tokens were consumed, False if insufficient tokens
        """
        async with self._lock:
            now = time.time()
            
            # Refill tokens based on elapsed time
            if now > self.last_refill:
                elapsed = now - self.last_refill
                tokens_to_add = (elapsed / self.refill_period) * self.refill_rate
                self.tokens = min(self.capacity, self.tokens + tokens_to_add)
                self.last_refill = now
            
            # Try to consume tokens
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            else:
                return False
    
    def get_retry_after(self) -> int:
        """Get seconds until next token is available"""
        if self.tokens >= 1:
            return 0
        
        tokens_needed = 1 - self.tokens
        time_needed = (tokens_needed / self.refill_rate) * self.refill_period
        return int(time_needed) + 1


class SlidingWindowCounter:
    """Sliding window counter for rate limiting"""
    
    def __init__(self, window_size: int, max_requests: int):
        """
        Initialize sliding window counter.
        
        Args:
            window_size: Window size in seconds
            max_requests: Maximum requests allowed in window
        """
        self.window_size = window_size
        self.max_requests = max_requests
        self.requests = deque()
        self._lock = asyncio.Lock()
    
    async def is_allowed(self) -> Tuple[bool, int]:
        """
        Check if request is allowed.
        
        Returns:
            Tuple of (is_allowed, retry_after_seconds)
        """
        async with self._lock:
            now = time.time()
            
            # Remove old requests outside the window
            while self.requests and self.requests[0] <= now - self.window_size:
                self.requests.popleft()
            
            # Check if we can allow this request
            if len(self.requests) < self.max_requests:
                self.requests.append(now)
                return True, 0
            else:
                # Calculate retry after time
                oldest_request = self.requests[0]
                retry_after = int(oldest_request + self.window_size - now) + 1
                return False, retry_after


class RateLimiter:
    """
    Advanced rate limiter supporting multiple algorithms and limits.
    """
    
    def __init__(self):
        # Different rate limiters for different scopes
        self.ip_limiters: Dict[str, TokenBucket] = {}
        self.endpoint_limiters: Dict[str, TokenBucket] = {}
        self.global_limiter: Optional[TokenBucket] = None
        self.session_limiters: Dict[str, SlidingWindowCounter] = {}
        
        # Configuration
        self.ip_rate_limits: Dict[str, Dict] = {}
        self.endpoint_rate_limits: Dict[str, Dict] = {}
        self.global_rate_limit: Optional[Dict] = None
        
        # Cleanup task
        self._cleanup_task: Optional[asyncio.Task] = None
        self._is_running = False
        
        # Statistics
        self.stats = {
            'total_requests': 0,
            'blocked_requests': 0,
            'ip_blocks': defaultdict(int),
            'endpoint_blocks': defaultdict(int),
            'reset_time': time.time()
        }
        
        logger.info("RateLimiter initialized")
    
    def configure_ip_rate_limit(self, requests_per_minute: int = 60, burst_size: int = 10):
        """Configure rate limiting per IP address"""
        self.ip_rate_limits['default'] = {
            'requests_per_minute': requests_per_minute,
            'burst_size': burst_size
        }
        logger.info(f"Configured IP rate limit: {requests_per_minute} req/min, burst: {burst_size}")
    
    def configure_endpoint_rate_limit(self, endpoint: str, requests_per_minute: int, burst_size: int):
        """Configure rate limiting for specific endpoint"""
        self.endpoint_rate_limits[endpoint] = {
            'requests_per_minute': requests_per_minute,
            'burst_size': burst_size
        }
        logger.info(f"Configured endpoint rate limit for {endpoint}: {requests_per_minute} req/min")
    
    def configure_global_rate_limit(self, requests_per_second: int):
        """Configure global rate limiting"""
        self.global_rate_limit = {
            'requests_per_second': requests_per_second
        }
        
        self.global_limiter = TokenBucket(
            capacity=requests_per_second * 2,  # Allow burst
            refill_rate=requests_per_second,
            refill_period=1.0
        )
        logger.info(f"Configured global rate limit: {requests_per_second} req/sec")
    
    async def start(self):
        """Start the rate limiter background tasks"""
        if self._is_running:
            return
        
        self._is_running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("RateLimiter started")
    
    async def stop(self):
        """Stop the rate limiter"""
        if not self._is_running:
            return
        
        self._is_running = False
        
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        
        logger.info("RateLimiter stopped")
    
    async def check_rate_limit(self, ip: str, endpoint: str, session_id: str = None) -> Tuple[bool, Dict[str, Any]]:
        """
        Check if request should be rate limited.
        
        Args:
            ip: Client IP address
            endpoint: API endpoint
            session_id: Session ID (optional)
            
        Returns:
            Tuple of (is_allowed, rate_limit_info)
        """
        self.stats['total_requests'] += 1
        
        rate_limit_info = {
            'ip': ip,
            'endpoint': endpoint,
            'timestamp': time.time(),
            'checks': {}
        }
        
        # Check global rate limit first
        if self.global_limiter:
            allowed = await self.global_limiter.consume()
            rate_limit_info['checks']['global'] = {
                'allowed': allowed,
                'retry_after': self.global_limiter.get_retry_after() if not allowed else 0
            }
            
            if not allowed:
                self.stats['blocked_requests'] += 1
                return False, rate_limit_info
        
        # Check IP-based rate limit
        if 'default' in self.ip_rate_limits:
            ip_limiter = await self._get_or_create_ip_limiter(ip)
            allowed = await ip_limiter.consume()
            rate_limit_info['checks']['ip'] = {
                'allowed': allowed,
                'retry_after': ip_limiter.get_retry_after() if not allowed else 0
            }
            
            if not allowed:
                self.stats['blocked_requests'] += 1
                self.stats['ip_blocks'][ip] += 1
                return False, rate_limit_info
        
        # Check endpoint-specific rate limit
        if endpoint in self.endpoint_rate_limits:
            endpoint_key = f"{ip}:{endpoint}"
            endpoint_limiter = await self._get_or_create_endpoint_limiter(endpoint_key, endpoint)
            allowed = await endpoint_limiter.consume()
            rate_limit_info['checks']['endpoint'] = {
                'allowed': allowed,
                'retry_after': endpoint_limiter.get_retry_after() if not allowed else 0
            }
            
            if not allowed:
                self.stats['blocked_requests'] += 1
                self.stats['endpoint_blocks'][endpoint] += 1
                return False, rate_limit_info
        
        # Check session-based limits if session_id provided
        if session_id:
            session_allowed, retry_after = await self._check_session_rate_limit(session_id)
            rate_limit_info['checks']['session'] = {
                'allowed': session_allowed,
                'retry_after': retry_after
            }
            
            if not session_allowed:
                self.stats['blocked_requests'] += 1
                return False, rate_limit_info
        
        return True, rate_limit_info
    
    async def _get_or_create_ip_limiter(self, ip: str) -> TokenBucket:
        """Get or create rate limiter for IP"""
        if ip not in self.ip_limiters:
            config = self.ip_rate_limits['default']
            self.ip_limiters[ip] = TokenBucket(
                capacity=config['burst_size'],
                refill_rate=config['requests_per_minute'] / 60.0,  # Convert to per second
                refill_period=1.0
            )
        return self.ip_limiters[ip]
    
    async def _get_or_create_endpoint_limiter(self, endpoint_key: str, endpoint: str) -> TokenBucket:
        """Get or create rate limiter for endpoint"""
        if endpoint_key not in self.endpoint_limiters:
            config = self.endpoint_rate_limits[endpoint]
            self.endpoint_limiters[endpoint_key] = TokenBucket(
                capacity=config['burst_size'],
                refill_rate=config['requests_per_minute'] / 60.0,  # Convert to per second
                refill_period=1.0
            )
        return self.endpoint_limiters[endpoint_key]
    
    async def _check_session_rate_limit(self, session_id: str) -> Tuple[bool, int]:
        """Check session-based rate limits"""
        if session_id not in self.session_limiters:
            # Default: 100 requests per 5 minutes per session
            self.session_limiters[session_id] = SlidingWindowCounter(
                window_size=300,  # 5 minutes
                max_requests=100
            )
        
        return await self.session_limiters[session_id].is_allowed()
    
    async def _cleanup_loop(self):
        """Background task to cleanup old rate limiters"""
        while self._is_running:
            try:
                await self._cleanup_old_limiters()
                await asyncio.sleep(300)  # Cleanup every 5 minutes
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in rate limiter cleanup: {e}", exc_info=True)
                await asyncio.sleep(300)
    
    async def _cleanup_old_limiters(self):
        """Remove old rate limiters to prevent memory leaks"""
        now = time.time()
        cleanup_age = 3600  # Remove limiters older than 1 hour
        
        # Clean up IP limiters
        old_ips = []
        for ip, limiter in self.ip_limiters.items():
            if now - limiter.last_refill > cleanup_age:
                old_ips.append(ip)
        
        for ip in old_ips:
            del self.ip_limiters[ip]
        
        # Clean up endpoint limiters
        old_endpoints = []
        for endpoint_key, limiter in self.endpoint_limiters.items():
            if now - limiter.last_refill > cleanup_age:
                old_endpoints.append(endpoint_key)
        
        for endpoint_key in old_endpoints:
            del self.endpoint_limiters[endpoint_key]
        
        # Clean up session limiters
        old_sessions = []
        for session_id, limiter in self.session_limiters.items():
            # Remove if no recent requests
            if not limiter.requests or now - limiter.requests[-1] > cleanup_age:
                old_sessions.append(session_id)
        
        for session_id in old_sessions:
            del self.session_limiters[session_id]
        
        if old_ips or old_endpoints or old_sessions:
            logger.debug(f"Cleaned up rate limiters: {len(old_ips)} IPs, {len(old_endpoints)} endpoints, {len(old_sessions)} sessions")
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get rate limiting statistics"""
        uptime = time.time() - self.stats['reset_time']
        
        return {
            'uptime_seconds': uptime,
            'total_requests': self.stats['total_requests'],
            'blocked_requests': self.stats['blocked_requests'],
            'block_rate': (self.stats['blocked_requests'] / max(self.stats['total_requests'], 1)) * 100,
            'requests_per_second': self.stats['total_requests'] / max(uptime, 1),
            'active_ip_limiters': len(self.ip_limiters),
            'active_endpoint_limiters': len(self.endpoint_limiters),
            'active_session_limiters': len(self.session_limiters),
            'top_blocked_ips': dict(list(self.stats['ip_blocks'].most_common(10))),
            'top_blocked_endpoints': dict(list(self.stats['endpoint_blocks'].most_common(10))),
            'configuration': {
                'ip_limits': self.ip_rate_limits,
                'endpoint_limits': self.endpoint_rate_limits,
                'global_limit': self.global_rate_limit
            }
        }
    
    def reset_statistics(self):
        """Reset rate limiting statistics"""
        self.stats = {
            'total_requests': 0,
            'blocked_requests': 0,
            'ip_blocks': defaultdict(int),
            'endpoint_blocks': defaultdict(int),
            'reset_time': time.time()
        }


class RateLimitMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware for rate limiting"""
    
    def __init__(self, app, requests_per_minute: int = 60, burst_size: int = 10):
        super().__init__(app)
        self.rate_limiter = RateLimiter()
        
        # Configure default rate limits
        self.rate_limiter.configure_ip_rate_limit(requests_per_minute, burst_size)
        
        # Start the rate limiter
        asyncio.create_task(self.rate_limiter.start())
    
    def configure_endpoint_limits(self, endpoint_limits: Dict[str, Dict[str, int]]):
        """Configure endpoint-specific rate limits"""
        for endpoint, config in endpoint_limits.items():
            self.rate_limiter.configure_endpoint_rate_limit(
                endpoint,
                config['requests_per_minute'],
                config['burst_size']
            )
    
    def configure_global_limit(self, requests_per_second: int):
        """Configure global rate limit"""
        self.rate_limiter.configure_global_rate_limit(requests_per_second)
    
    async def dispatch(self, request: Request, call_next):
        # Get client information
        client_ip = self._get_client_ip(request)
        endpoint = self._get_endpoint_key(request)
        session_id = self._get_session_id(request)
        
        # Check rate limits
        allowed, rate_limit_info = await self.rate_limiter.check_rate_limit(
            ip=client_ip,
            endpoint=endpoint,
            session_id=session_id
        )
        
        if not allowed:
            # Find the most restrictive retry-after time
            retry_after = 0
            blocked_by = "rate_limit"
            
            for check_name, check_info in rate_limit_info['checks'].items():
                if not check_info['allowed'] and check_info['retry_after'] > retry_after:
                    retry_after = check_info['retry_after']
                    blocked_by = check_name
            
            # Log the rate limit violation
            logger.warning(f"Rate limit exceeded for {client_ip} on {endpoint} (blocked by {blocked_by})")
            
            # Return rate limit error response
            return JSONResponse(
                status_code=429,
                content={
                    "error": "Rate limit exceeded",
                    "retry_after": retry_after,
                    "blocked_by": blocked_by,
                    "client_ip": client_ip,
                    "endpoint": endpoint
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Blocked-By": blocked_by
                }
            )
        
        # Process the request
        response = await call_next(request)
        
        # Add rate limit headers to response
        response.headers["X-RateLimit-Remaining"] = "1"  # Simplified
        response.headers["X-RateLimit-Reset"] = str(int(time.time() + 60))
        
        return response
    
    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP from request"""
        # Check for forwarded headers first
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip
        
        # Fallback to client host
        if request.client:
            return request.client.host
        
        return "unknown"
    
    def _get_endpoint_key(self, request: Request) -> str:
        """Get endpoint identifier for rate limiting"""
        # Combine method and path, but normalize dynamic parts
        method = request.method
        path = request.url.path
        
        # Normalize paths with IDs (e.g., /sessions/123 -> /sessions/{id})
        import re
        path = re.sub(r'/[a-f0-9-]{36}', '/{uuid}', path)  # UUIDs
        path = re.sub(r'/\d+', '/{id}', path)  # Numeric IDs
        
        return f"{method} {path}"
    
    def _get_session_id(self, request: Request) -> Optional[str]:
        """Extract session ID from request"""
        # Try to get from Authorization header
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            return auth_header.split(" ")[1]
        
        # Try to get from cookie
        session_cookie = request.cookies.get("session_id")
        if session_cookie:
            return session_cookie
        
        # Try to get from query parameter
        session_param = request.query_params.get("session_id")
        if session_param:
            return session_param
        
        return None


class AdaptiveRateLimiter:
    """
    Adaptive rate limiter that adjusts limits based on system load.
    """
    
    def __init__(self, base_rate_limiter: RateLimiter):
        self.base_limiter = base_rate_limiter
        self.adaptation_factor = 1.0
        self.load_history = deque(maxlen=60)  # Last 60 measurements
        self.last_adaptation = time.time()
        
    async def adapt_limits(self, system_load: float, error_rate: float):
        """
        Adapt rate limits based on system metrics.
        
        Args:
            system_load: System load (0.0 to 1.0)
            error_rate: Error rate (0.0 to 1.0)
        """
        now = time.time()
        
        # Only adapt every 30 seconds
        if now - self.last_adaptation < 30:
            return
        
        self.load_history.append({
            'timestamp': now,
            'system_load': system_load,
            'error_rate': error_rate
        })
        
        # Calculate adaptation factor
        if len(self.load_history) >= 5:
            avg_load = sum(h['system_load'] for h in list(self.load_history)[-5:]) / 5
            avg_error_rate = sum(h['error_rate'] for h in list(self.load_history)[-5:]) / 5
            
            # Reduce limits if system is under stress
            if avg_load > 0.8 or avg_error_rate > 0.05:
                self.adaptation_factor = max(0.1, self.adaptation_factor * 0.8)
                logger.warning(f"Reducing rate limits due to high load (factor: {self.adaptation_factor:.2f})")
            elif avg_load < 0.5 and avg_error_rate < 0.01:
                self.adaptation_factor = min(2.0, self.adaptation_factor * 1.1)
                logger.info(f"Increasing rate limits due to low load (factor: {self.adaptation_factor:.2f})")
        
        self.last_adaptation = now
    
    def get_adapted_limit(self, base_limit: int) -> int:
        """Get adapted rate limit"""
        return max(1, int(base_limit * self.adaptation_factor))


class CustomRateLimitRules:
    """
    Custom rate limiting rules for different user types and endpoints.
    """
    
    def __init__(self):
        self.rules: Dict[str, Dict[str, Any]] = {}
        self.user_tiers: Dict[str, str] = {}  # user_id -> tier
        self.ip_whitelist: Set[str] = set()
        self.ip_blacklist: Set[str] = set()
    
    def add_user_tier(self, user_id: str, tier: str):
        """Add user to a specific tier"""
        self.user_tiers[user_id] = tier
    
    def add_tier_rules(self, tier: str, rules: Dict[str, Any]):
        """
        Add rate limiting rules for a tier.
        
        Args:
            tier: Tier name (e.g., 'free', 'premium', 'enterprise')
            rules: Rules dictionary with limits
        """
        self.rules[tier] = rules
    
    def whitelist_ip(self, ip: str):
        """Add IP to whitelist (no rate limiting)"""
        self.ip_whitelist.add(ip)
    
    def blacklist_ip(self, ip: str):
        """Add IP to blacklist (always blocked)"""
        self.ip_blacklist.add(ip)
    
    def get_limits_for_request(self, user_id: str = None, ip: str = None) -> Optional[Dict[str, Any]]:
        """Get rate limits for a specific request"""
        # Check blacklist first
        if ip and ip in self.ip_blacklist:
            return {'blocked': True, 'reason': 'blacklisted'}
        
        # Check whitelist
        if ip and ip in self.ip_whitelist:
            return {'unlimited': True}
        
        # Get user tier
        if user_id and user_id in self.user_tiers:
            tier = self.user_tiers[user_id]
            if tier in self.rules:
                return self.rules[tier]
        
        # Return default limits
        return self.rules.get('default', None)


# Global rate limiter instance
_global_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    """Get the global rate limiter instance"""
    global _global_rate_limiter
    if _global_rate_limiter is None:
        _global_rate_limiter = RateLimiter()
    return _global_rate_limiter


async def init_rate_limiter(
    ip_requests_per_minute: int = 60,
    ip_burst_size: int = 10,
    global_requests_per_second: int = None,
    endpoint_limits: Dict[str, Dict[str, int]] = None
) -> RateLimiter:
    """Initialize the global rate limiter with configuration"""
    global _global_rate_limiter
    
    _global_rate_limiter = RateLimiter()
    
    # Configure IP-based limits
    _global_rate_limiter.configure_ip_rate_limit(ip_requests_per_minute, ip_burst_size)
    
    # Configure global limits if specified
    if global_requests_per_second:
        _global_rate_limiter.configure_global_rate_limit(global_requests_per_second)
    
    # Configure endpoint-specific limits
    if endpoint_limits:
        for endpoint, config in endpoint_limits.items():
            _global_rate_limiter.configure_endpoint_rate_limit(
                endpoint,
                config['requests_per_minute'],
                config['burst_size']
            )
    
    await _global_rate_limiter.start()
    
    logger.info("Global rate limiter initialized and started")
    return _global_rate_limiter


async def shutdown_rate_limiter():
    """Shutdown the global rate limiter"""
    global _global_rate_limiter
    if _global_rate_limiter is not None:
        await _global_rate_limiter.stop()
        _global_rate_limiter = None


# Utility decorators for rate limiting
def rate_limit(requests_per_minute: int = 60, burst_size: int = 10):
    """Decorator for rate limiting specific endpoints"""
    def decorator(func):
        # Store rate limit configuration on the function
        func._rate_limit_config = {
            'requests_per_minute': requests_per_minute,
            'burst_size': burst_size
        }
        return func
    return decorator


def requires_rate_limit_check():
    """Decorator to enforce rate limit checking"""
    def decorator(func):
        async def wrapper(*args, **kwargs):
            # This would be implemented with dependency injection
            # in a real FastAPI application
            return await func(*args, **kwargs)
        return wrapper
    return decorator


# Helper functions for common rate limiting patterns
async def check_api_rate_limit(request: Request) -> bool:
    """Check API rate limits for a request"""
    rate_limiter = get_rate_limiter()
    client_ip = request.client.host if request.client else "unknown"
    endpoint = f"{request.method} {request.url.path}"
    
    allowed, _ = await rate_limiter.check_rate_limit(client_ip, endpoint)
    return allowed


async def apply_burst_protection(ip: str, max_burst: int = 5, window_seconds: int = 10) -> bool:
    """Apply burst protection for an IP address"""
    # This would typically use a dedicated burst protector
    # For now, use the global rate limiter
    rate_limiter = get_rate_limiter()
    allowed, _ = await rate_limiter.check_rate_limit(ip, "burst_protection")
    return allowed


def create_custom_rate_limit_response(retry_after: int, message: str = None) -> JSONResponse:
    """Create a custom rate limit exceeded response"""
    content = {
        "error": "Rate limit exceeded",
        "message": message or "Too many requests. Please try again later.",
        "retry_after": retry_after,
        "timestamp": datetime.now().isoformat()
    }
    
    headers = {
        "Retry-After": str(retry_after),
        "X-RateLimit-Retry-After": str(retry_after)
    }
    
    return JSONResponse(
        status_code=429,
        content=content,
        headers=headers
    )


# Export main components
__all__ = [
    'RateLimitExceeded',
    'TokenBucket',
    'SlidingWindowCounter', 
    'RateLimiter',
    'RateLimitMiddleware',
    'AdaptiveRateLimiter',
    'CustomRateLimitRules',
    'get_rate_limiter',
    'init_rate_limiter',
    'shutdown_rate_limiter',
    'rate_limit',
    'requires_rate_limit_check',
    'check_api_rate_limit',
    'apply_burst_protection',
    'create_custom_rate_limit_response'
]