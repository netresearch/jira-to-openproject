#!/usr/bin/env python3
"""Enhanced OpenProject client with advanced features for migration operations."""

import logging
from typing import Any, Dict, List, Optional

from src.display import configure_logging
from src.clients.openproject_client import OpenProjectClient

logger = configure_logging("INFO", None)


class EnhancedOpenProjectClient(OpenProjectClient):
    """Enhanced OpenProject client with additional migration-specific features."""
    
    def __init__(self, **kwargs):
        """Initialize the enhanced OpenProject client."""
        super().__init__(**kwargs)
        self._enhanced_features_enabled = True
    
    def get_enhanced_users(self, **kwargs) -> List[Dict[str, Any]]:
        """Get users with enhanced metadata for migration."""
        # Enhanced implementation would go here
        return self.get_users(**kwargs)
    
    def get_enhanced_projects(self, **kwargs) -> List[Dict[str, Any]]:
        """Get projects with enhanced metadata for migration."""
        # Enhanced implementation would go here
        return self.get_projects(**kwargs) 