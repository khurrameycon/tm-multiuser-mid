"""
Configuration module for multi-user browser automation API.

This module provides centralized configuration management with environment-based
settings, validation, and easy access to configuration values throughout the application.
"""

from .settings import (
    Settings,
    get_settings,
    reload_settings,
    update_setting,
    get_environment_info,
    DevelopmentSettings,
    ProductionSettings,
    StagingSettings,
    get_settings_class_for_environment
)

# Export main configuration interface
__all__ = [
    # Main settings class and functions
    'Settings',
    'get_settings',
    'reload_settings',
    'update_setting',
    'get_environment_info',
    
    # Environment-specific settings
    'DevelopmentSettings',
    'ProductionSettings', 
    'StagingSettings',
    'get_settings_class_for_environment',
    
    # Convenience functions
    'get_log_config',
    'get_resource_limits',
    'get_feature_flags',
    'is_development',
    'is_production',
]


def get_log_config():
    """Get logging configuration from settings"""
    return get_settings().get_log_config()


def get_resource_limits():
    """Get resource limits from settings"""
    return get_settings().get_resource_limits()


def get_feature_flags():
    """Get feature flags from settings"""
    return get_settings().get_feature_flags()


def is_development():
    """Check if running in development environment"""
    return get_settings().is_development()


def is_production():
    """Check if running in production environment"""
    return get_settings().is_production()


# Initialize settings on module import
try:
    _settings = get_settings()
    print(f"Configuration loaded: {_settings.environment} environment")
except Exception as e:
    print(f"Warning: Failed to load configuration: {e}")
    print("Using default settings")