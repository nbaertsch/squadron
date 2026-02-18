# Enhanced agent manager methods for better watchdog reliability

# Role-specific cleanup timeouts 
CLEANUP_TIMEOUTS = {
    "pr-review": 60,      # Git operations and PR comments can take time
    "infra-dev": 90,      # Complex infrastructure operations
    "security-review": 60, # Security analysis can be intensive  
    "feat-dev": 45,       # Feature development with multiple files
    "default": 30         # Conservative default
}
