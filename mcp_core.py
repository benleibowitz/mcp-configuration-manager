#!/usr/bin/env python3
"""
MCP Core Engine - Core classes and functionality for MCP configuration management.

This module contains the core synchronization engine, format handlers, and daemon
functionality for managing Model Context Protocol (MCP) server configurations
across multiple applications.
"""

import json
import os
from pathlib import Path
import logging
from datetime import datetime
import sys
import time
import signal
import threading
from abc import ABC, abstractmethod
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm
from rich import box

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configure Rich console
console = Console()

class ConfigFormatHandler(ABC):
    """Abstract base class for handling different MCP configuration formats."""
    
    @abstractmethod
    def detect_format(self, config_data: dict) -> bool:
        """Detect if this handler can process the given configuration format."""
        pass
    
    @abstractmethod
    def extract_mcp_config(self, config_data: dict) -> dict:
        """Extract MCP configuration from the app-specific format."""
        pass
    
    @abstractmethod
    def merge_mcp_config(self, existing_config: dict, mcp_config: dict) -> dict:
        """Merge MCP configuration back into the app-specific format."""
        pass
    
    @abstractmethod
    def get_format_name(self) -> str:
        """Get the name of this configuration format."""
        pass

class ClaudeDesktopHandler(ConfigFormatHandler):
    """Handler for Claude Desktop's mcpServers configuration format."""
    
    def detect_format(self, config_data: dict) -> bool:
        return 'mcpServers' in config_data
    
    def extract_mcp_config(self, config_data: dict) -> dict:
        """Convert Claude Desktop's mcpServers to normalized MCP config."""
        mcp_servers = config_data.get('mcpServers', {})
        
        # Create a normalized representation
        normalized_config = {
            'format': 'claude_desktop',
            'servers': mcp_servers
        }
        
        return normalized_config
    
    def merge_mcp_config(self, existing_config: dict, mcp_config: dict) -> dict:
        """Merge MCP config back into Claude Desktop format."""
        updated_config = existing_config.copy()
        
        # If the MCP config is in normalized format, extract servers
        if isinstance(mcp_config, dict) and 'servers' in mcp_config:
            updated_config['mcpServers'] = mcp_config['servers']
        elif isinstance(mcp_config, dict) and 'mcpServers' in mcp_config:
            updated_config['mcpServers'] = mcp_config['mcpServers']
        else:
            # Handle legacy format by wrapping in mcpServers
            updated_config['mcpServers'] = mcp_config
        
        return updated_config
    
    def get_format_name(self) -> str:
        return "Claude Desktop (mcpServers)"

class StandardMCPHandler(ConfigFormatHandler):
    """Handler for the standard MCP configuration format used by other apps."""
    
    def detect_format(self, config_data: dict) -> bool:
        return 'mcp' in config_data
    
    def extract_mcp_config(self, config_data: dict) -> dict:
        """Extract MCP configuration from standard format."""
        return config_data.get('mcp', {})
    
    def merge_mcp_config(self, existing_config: dict, mcp_config: dict) -> dict:
        """Merge MCP configuration into standard format."""
        updated_config = existing_config.copy()
        updated_config['mcp'] = mcp_config
        return updated_config
    
    def get_format_name(self) -> str:
        return "Standard MCP"

class VSCodeHandler(ConfigFormatHandler):
    """Handler for VSCode's settings.json mcp.servers configuration format."""
    
    def detect_format(self, config_data: dict) -> bool:
        return 'mcp' in config_data and isinstance(config_data['mcp'], dict) and 'servers' in config_data['mcp']
    
    def extract_mcp_config(self, config_data: dict) -> dict:
        """Extract MCP configuration from VSCode settings format."""
        mcp_section = config_data.get('mcp', {})
        servers = mcp_section.get('servers', {})
        
        # Create a normalized representation similar to Claude Desktop
        normalized_config = {
            'format': 'vscode',
            'servers': servers,
            'inputs': mcp_section.get('inputs', [])
        }
        
        return normalized_config
    
    def merge_mcp_config(self, existing_config: dict, mcp_config: dict) -> dict:
        """Merge MCP config back into VSCode settings format."""
        updated_config = existing_config.copy()
        
        # Initialize mcp section if it doesn't exist
        if 'mcp' not in updated_config:
            updated_config['mcp'] = {}
        
        # Handle different input formats
        if isinstance(mcp_config, dict) and 'servers' in mcp_config:
            # Normalized format from VSCode or Claude Desktop
            updated_config['mcp']['servers'] = mcp_config['servers']
            if 'inputs' in mcp_config:
                updated_config['mcp']['inputs'] = mcp_config['inputs']
        elif isinstance(mcp_config, dict) and 'mcpServers' in mcp_config:
            # Claude Desktop format
            updated_config['mcp']['servers'] = mcp_config['mcpServers']
        else:
            # Legacy format - wrap servers in VSCode structure
            updated_config['mcp']['servers'] = mcp_config
            
        # Ensure inputs exists
        if 'inputs' not in updated_config['mcp']:
            updated_config['mcp']['inputs'] = []
        
        return updated_config
    
    def get_format_name(self) -> str:
        return "VSCode (mcp.servers)"

class CursorHandler(ConfigFormatHandler):
    """Handler for Cursor's mixed mcpServers + mcp.servers configuration format."""
    
    def detect_format(self, config_data: dict) -> bool:
        """Detect Cursor's specific mixed format with both mcpServers and mcp sections."""
        return ('mcpServers' in config_data and 
                'mcp' in config_data and 
                isinstance(config_data['mcp'], dict))
    
    def extract_mcp_config(self, config_data: dict) -> dict:
        """Extract MCP configuration from Cursor's mixed format, preferring mcp.servers."""
        # Prefer the mcp section if it exists, as it's the newer format
        if 'mcp' in config_data and isinstance(config_data['mcp'], dict):
            return config_data['mcp']
        
        # Fallback to mcpServers if mcp section is missing/invalid
        mcp_servers = config_data.get('mcpServers', {})
        return {
            'format': 'cursor_legacy',
            'servers': mcp_servers
        }
    
    def merge_mcp_config(self, existing_config: dict, mcp_config: dict) -> dict:
        """Merge MCP config into Cursor format, cleaning up legacy mcpServers."""
        updated_config = existing_config.copy()
        
        # Use standard MCP format for the new section
        updated_config['mcp'] = mcp_config
        
        # Remove legacy mcpServers section to avoid conflicts
        if 'mcpServers' in updated_config:
            logger.debug("Removing legacy mcpServers section from Cursor config")
            del updated_config['mcpServers']
        
        return updated_config
    
    def get_format_name(self) -> str:
        return "Cursor (mixed)"

class LegacyMCPHandler(ConfigFormatHandler):
    """Handler for legacy/empty configurations that need to be initialized."""
    
    def detect_format(self, config_data: dict) -> bool:
        # This handler accepts any config that doesn't match other formats
        return True
    
    def extract_mcp_config(self, config_data: dict) -> dict:
        """Return empty MCP config for legacy/empty configurations."""
        return {}
    
    def merge_mcp_config(self, existing_config: dict, mcp_config: dict) -> dict:
        """Merge MCP configuration using standard format."""
        updated_config = existing_config.copy()
        updated_config['mcp'] = mcp_config
        return updated_config
    
    def get_format_name(self) -> str:
        return "Legacy/Empty"

class MCPConfigWatcher(FileSystemEventHandler):
    """File system event handler for watching MCP configuration changes."""
    
    def __init__(self, synchronizer, debounce_delay=2.0):
        super().__init__()
        self.synchronizer = synchronizer
        self.debounce_delay = debounce_delay
        self.pending_syncs = {}
        self.lock = threading.Lock()
        
    def on_modified(self, event):
        if event.is_directory:
            return
            
        file_path = Path(event.src_path)
        
        # Check if this is one of our monitored config files
        source_app = None
        for app_name, config_path in self.synchronizer.CONFIG_FILES.items():
            try:
                if file_path.exists() and config_path.exists() and file_path.samefile(config_path):
                    source_app = app_name
                    break
            except (OSError, FileNotFoundError):
                # File might have been deleted or moved, skip
                continue
        
        if source_app:
            # Check if this change was caused by our own sync operation
            if self._is_sync_in_progress(source_app):
                logger.debug(f"Ignoring self-triggered change in {source_app} config")
                return
                
            logger.info(f"Detected external change in {source_app} config: {file_path}")
            self._schedule_sync(source_app, file_path)
    
    def _is_sync_in_progress(self, app_name):
        """Check if a sync operation is currently in progress for this app."""
        # Simple check - if there's a pending sync, assume we might be in the middle of it
        with self.lock:
            return app_name in self.pending_syncs
    
    def _schedule_sync(self, source_app, file_path):
        """Schedule a sync with debouncing to avoid rapid successive syncs."""
        with self.lock:
            # Cancel any existing timer for this app
            if source_app in self.pending_syncs:
                self.pending_syncs[source_app].cancel()
            
            # Schedule new sync
            timer = threading.Timer(
                self.debounce_delay, 
                self._execute_sync, 
                args=(source_app, file_path)
            )
            timer.start()
            self.pending_syncs[source_app] = timer
    
    def _execute_sync(self, source_app, file_path):
        """Execute the actual sync operation."""
        try:
            logger.info(f"Starting automatic sync from {source_app}")
            success = self.synchronizer.sync_from_file(source_app)
            
            if success:
                logger.info(f"Automatic sync from {source_app} completed successfully")
            else:
                logger.error(f"Automatic sync from {source_app} failed")
                
        except Exception as e:
            logger.error(f"Error during automatic sync from {source_app}: {e}")
        finally:
            # Clean up the timer reference
            with self.lock:
                self.pending_syncs.pop(source_app, None)

class MCPSyncDaemon:
    """Daemon for running continuous MCP configuration synchronization."""
    
    def __init__(self, synchronizer, watch_apps=None, debounce_delay=2.0):
        self.synchronizer = synchronizer
        self.watch_apps = watch_apps or list(synchronizer.CONFIG_FILES.keys())
        self.debounce_delay = debounce_delay
        self.observer = Observer()
        self.event_handler = MCPConfigWatcher(synchronizer, debounce_delay)
        self.running = False
        
    def start(self):
        """Start the file watching daemon."""
        logger.info("Starting MCP Config Sync Daemon")
        logger.info(f"Watching apps: {', '.join(self.watch_apps)}")
        logger.info(f"Debounce delay: {self.debounce_delay}s")
        
        # Setup file watchers for each monitored app
        watched_paths = set()
        for app_name in self.watch_apps:
            if app_name in self.synchronizer.CONFIG_FILES:
                config_path = self.synchronizer.CONFIG_FILES[app_name]
                
                # Watch the parent directory since the file might not exist yet
                watch_dir = config_path.parent
                if watch_dir not in watched_paths:
                    self.observer.schedule(
                        self.event_handler, 
                        str(watch_dir), 
                        recursive=False
                    )
                    watched_paths.add(watch_dir)
                    logger.info(f"Watching directory: {watch_dir}")
        
        # Start the observer
        self.observer.start()
        self.running = True
        
        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        logger.info("Daemon started. Press Ctrl+C to stop.")
        
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
    
    def stop(self):
        """Stop the file watching daemon."""
        if self.running:
            logger.info("Stopping MCP Config Sync Daemon")
            self.running = False
            self.observer.stop()
            self.observer.join()
            logger.info("Daemon stopped")
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

class MCPConfigSynchronizer:
    """Synchronizes MCP configuration across multiple application config files."""
    
    CONFIG_FILES = {
        'Cursor': Path.home() / '.cursor' / 'mcp.json',
        'Windsurf': Path.home() / '.codeium' / 'windsurf' / 'mcp_config.json',
        'Roocode-VSCode': Path.home() / 'Library' / 'Application Support' / 'Code' / 'User' / 
                  'globalStorage' / 'rooveterinaryinc.roo-cline' / 'settings' / 'cline_mcp_settings.json',
        'Roocode-Windsurf': Path.home() / 'Library' / 'Application Support' / 'Windsurf - Next' / 'User' /
                  'globalStorage' / 'rooveterinaryinc.roo-cline' / 'settings' / 'mcp_settings.json',
        'Claude': Path.home() / 'Library' / 'Application Support' / 'Claude' / 'claude_desktop_config.json',
        'VSCode': Path.home() / 'Library' / 'Application Support' / 'Code' / 'User' / 'settings.json'
    }
    
    # Configuration format handlers (order matters - most specific first)
    FORMAT_HANDLERS = [
        ClaudeDesktopHandler(),
        VSCodeHandler(),
        CursorHandler(),
        StandardMCPHandler(),
        LegacyMCPHandler()  # Fallback handler
    ]
    
    # Map applications to their preferred handlers
    APP_HANDLERS = {
        'Claude': ClaudeDesktopHandler(),
        'VSCode': VSCodeHandler(),
        'Cursor': CursorHandler(),
        'Windsurf': StandardMCPHandler(),
        'Roocode-VSCode': StandardMCPHandler(),
        'Roocode-Windsurf': StandardMCPHandler()
    }
    
    DEFAULT_MCP_CONFIG = {
        'servers': {}
    }
    
    def __init__(self):
        self.config = self.DEFAULT_MCP_CONFIG.copy()
        self.sync_results = {}
        # Filter CONFIG_FILES to only include apps that are actually installed
        self._filter_installed_apps()
    
    def _filter_installed_apps(self):
        """Filter CONFIG_FILES to only include applications that are actually installed."""
        installed_apps = {}
        
        for app_name, config_path in self.CONFIG_FILES.items():
            # Check if the application directory exists
            # For most apps, we check if the parent directory (where the app stores configs) exists
            app_dir = None
            
            if app_name == 'Claude':
                app_dir = config_path.parent  # ~/Library/Application Support/Claude/
            elif app_name == 'VSCode':
                app_dir = config_path.parent.parent  # ~/Library/Application Support/Code/
            elif app_name == 'Cursor':
                app_dir = config_path.parent  # ~/.cursor/
            elif app_name == 'Windsurf':
                app_dir = config_path.parent.parent  # ~/.codeium/windsurf/
            elif app_name.startswith('Roocode'):
                # For Roocode variants, check if the base application directory exists
                if 'VSCode' in app_name:
                    app_dir = Path.home() / 'Library' / 'Application Support' / 'Code'
                elif 'Windsurf' in app_name:
                    app_dir = Path.home() / 'Library' / 'Application Support' / 'Windsurf - Next'
            
            # Include the app if its directory exists (indicating it's installed)
            if app_dir and app_dir.exists():
                installed_apps[app_name] = config_path
                logger.debug(f"Application {app_name} detected at {app_dir}")
            else:
                logger.debug(f"Application {app_name} not found (directory {app_dir} does not exist)")
        
        # Update CONFIG_FILES with only installed applications
        self.CONFIG_FILES = installed_apps
        logger.info(f"Detected {len(installed_apps)} installed applications: {', '.join(installed_apps.keys())}")
    
    def detect_config_format(self, config_data: dict) -> ConfigFormatHandler:
        """Detect the appropriate format handler for the given configuration."""
        for handler in self.FORMAT_HANDLERS:
            if handler.detect_format(config_data):
                return handler
        # Should never reach here due to LegacyMCPHandler fallback
        return LegacyMCPHandler()
    
    def get_app_handler(self, app_name: str) -> ConfigFormatHandler:
        """Get the appropriate format handler for a specific application."""
        return self.APP_HANDLERS.get(app_name, StandardMCPHandler())
    
    def ensure_directories(self):
        """Ensure all parent directories for config files exist."""
        for config_path in self.CONFIG_FILES.values():
            config_path.parent.mkdir(parents=True, exist_ok=True)
            logger.info(f"Ensured directory exists: {config_path.parent}")
    
    def load_existing_config(self, config_path):
        """Load existing configuration from a file if it exists."""
        try:
            if config_path.exists():
                with open(config_path, 'r') as f:
                    return json.load(f)
            return {}
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse config at {config_path}: {e}")
            # Return None to indicate a parsing error, not just an empty config
            return None
        except Exception as e:
            logger.error(f"Error loading config at {config_path}: {e}")
            return None
    
    def merge_configs(self, existing_config, new_config):
        """Merge existing config with new config, preserving existing values where applicable."""
        def deep_merge(d1, d2):
            for key, value in d2.items():
                if isinstance(value, dict) and key in d1:
                    d1[key] = deep_merge(d1.get(key, {}), value)
                else:
                    d1[key] = value
            return d1
        
        return deep_merge(existing_config.copy(), new_config)
    
    def check_destructive_operations(self):
        """Check if the sync operation would remove existing MCP servers."""
        destructive_apps = []
        source_servers = self.config.get('servers', {})
        
        for app_name, config_path in self.CONFIG_FILES.items():
            if not config_path.exists():
                continue
                
            existing_config = self.load_existing_config(config_path)
            if existing_config is None:
                continue
                
            # Extract existing MCP servers
            handler = self.detect_config_format(existing_config)
            existing_mcp_config = handler.extract_mcp_config(existing_config)
            existing_servers = existing_mcp_config.get('servers', {})
            
            # Check if we're removing servers
            if existing_servers and len(existing_servers) > len(source_servers):
                lost_servers = set(existing_servers.keys()) - set(source_servers.keys())
                if lost_servers:
                    destructive_apps.append({
                        'app_name': app_name,
                        'existing_servers': list(existing_servers.keys()),
                        'lost_servers': list(lost_servers),
                        'remaining_servers': list(source_servers.keys())
                    })
        
        return destructive_apps
    
    def prompt_user_confirmation(self, destructive_apps):
        """Prompt user for confirmation of destructive operations."""
        console.print()
        
        # Create a table for destructive operations
        table = Table(
            title="⚠️  Destructive Operation Detected",
            box=box.ROUNDED,
            title_style="bold red",
            show_header=True,
            header_style="bold magenta"
        )
        table.add_column("Application", style="cyan", no_wrap=True)
        table.add_column("Current Servers", style="green")
        table.add_column("Servers to Remove", style="red")
        table.add_column("Remaining Servers", style="yellow")
        
        for app_info in destructive_apps:
            current = ', '.join(app_info['existing_servers']) if app_info['existing_servers'] else 'none'
            removed = ', '.join(app_info['lost_servers']) if app_info['lost_servers'] else 'none'
            remaining = ', '.join(app_info['remaining_servers']) if app_info['remaining_servers'] else 'none'
            
            table.add_row(
                f"📱 {app_info['app_name']}", 
                current, 
                removed, 
                remaining
            )
        
        console.print(table)
        console.print()
        
        # Use rich Confirm for confirmation
        try:
            return Confirm.ask(
                "Do you want to continue with this destructive operation?",
                default=False,
                console=console
            )
        except (KeyboardInterrupt, EOFError):
            console.print("\n[red]Operation cancelled by user[/red]")
            return False

    def update_configs(self, custom_config=None, force=False):
        """Update all configuration files with the specified MCP configuration."""
        self.ensure_directories()
        
        if custom_config:
            self.config = self.merge_configs(self.config, custom_config)
        
        # Check for destructive operations
        destructive_apps = self.check_destructive_operations()
        if destructive_apps and not force:
            if not self.prompt_user_confirmation(destructive_apps):
                logger.info("Operation cancelled by user")
                return {app_name: {'success': False, 'action': 'cancelled', 'reason': 'user_cancelled'} 
                       for app_name in self.CONFIG_FILES.keys()}
        
        results = {}
        for app_name, config_path in self.CONFIG_FILES.items():
            try:
                # Load existing config to preserve any app-specific settings
                existing_config = self.load_existing_config(config_path)
                
                # If parsing failed, skip this config
                if existing_config is None:
                    logger.error(f"Skipping update for {app_name} due to parsing error")
                    results[app_name] = {
                        'success': False, 
                        'path': config_path,
                        'error': 'Failed to parse existing config',
                        'action': 'skipped'
                    }
                    continue
                
                # Get file status before update
                file_existed = config_path.exists()
                
                # Get the appropriate handler for this app
                handler = self.get_app_handler(app_name)
                
                # Merge with new MCP config using format-specific handler
                updated_config = handler.merge_mcp_config(existing_config, self.config)
                
                # Write updated config
                with open(config_path, 'w') as f:
                    json.dump(updated_config, f, indent=2)
                
                # Record result
                action = 'updated' if file_existed else 'created'
                logger.info(f"Successfully {action} config for {app_name} at {config_path} using {handler.get_format_name()} format")
                results[app_name] = {
                    'success': True, 
                    'path': config_path,
                    'action': action,
                    'size': config_path.stat().st_size,
                    'format': handler.get_format_name()
                }
                
            except Exception as e:
                logger.error(f"Failed to update config for {app_name} at {config_path}: {e}")
                results[app_name] = {
                    'success': False, 
                    'path': config_path,
                    'error': str(e),
                    'action': 'failed'
                }
        
        return results
    
    def validate_configs(self, reference_config=None):
        """Validate that all configuration files are in sync and properly formatted."""
        if reference_config is None:
            reference_config = self.config
        
        all_in_sync = True
        validation_results = {}
        
        for app_name, config_path in self.CONFIG_FILES.items():
            if not config_path.exists():
                logger.warning(f"Config file missing for {app_name} at {config_path}")
                validation_results[app_name] = {'in_sync': False, 'reason': 'missing'}
                all_in_sync = False
                continue
                
            config = self.load_existing_config(config_path)
            if config is None:
                logger.warning(f"Config file for {app_name} at {config_path} could not be parsed")
                validation_results[app_name] = {'in_sync': False, 'reason': 'parse_error'}
                all_in_sync = False
                continue
            
            # Use format-specific handler to extract MCP config for comparison
            handler = self.detect_config_format(config)
            mcp_config = handler.extract_mcp_config(config)
            
            # For Claude Desktop format, we need to compare the servers structure
            if isinstance(handler, ClaudeDesktopHandler):
                # Extract servers from both configurations for comparison
                ref_servers = reference_config.get('servers', {}) if isinstance(reference_config, dict) and 'servers' in reference_config else {}
                app_servers = mcp_config.get('servers', {}) if isinstance(mcp_config, dict) and 'servers' in mcp_config else {}
                
                # If reference config is in legacy format, we can't do meaningful comparison
                if not ref_servers and reference_config:
                    logger.info(f"Skipping validation for {app_name} - reference config is in legacy format, app uses Claude Desktop format")
                    validation_results[app_name] = {'in_sync': True, 'reason': 'format_mismatch_skip'}
                    continue
                
                # Compare server configurations
                is_in_sync = app_servers == ref_servers
                if not is_in_sync:
                    mismatched_keys = ['servers (content mismatch)']
                else:
                    mismatched_keys = []
            else:
                # Standard validation for other formats
                is_in_sync = True
                mismatched_keys = []
                
                def check_nested_dict(ref_dict, app_dict, path=""):
                    nonlocal is_in_sync, mismatched_keys
                    for key, ref_value in ref_dict.items():
                        # Skip format field as it's metadata, not actual config data
                        if key == 'format':
                            continue
                            
                        if key not in app_dict:
                            is_in_sync = False
                            mismatched_keys.append(f"{path}{key} (missing)")
                            continue
                            
                        app_value = app_dict[key]
                        if isinstance(ref_value, dict) and isinstance(app_value, dict):
                            check_nested_dict(ref_value, app_value, f"{path}{key}.")
                        elif ref_value != app_value:
                            is_in_sync = False
                            mismatched_keys.append(f"{path}{key} (value mismatch)")
                
                check_nested_dict(reference_config, mcp_config)
            
            if not is_in_sync:
                logger.warning(f"Config mismatch detected for {app_name} at {config_path}")
                logger.warning(f"Mismatched keys: {', '.join(mismatched_keys)}")
                logger.debug(f"Reference config for {app_name}: {reference_config}")
                logger.debug(f"App config for {app_name}: {mcp_config}")
                validation_results[app_name] = {
                    'in_sync': False, 
                    'reason': 'mismatch',
                    'mismatched_keys': mismatched_keys,
                    'format': handler.get_format_name()
                }
                all_in_sync = False
            else:
                validation_results[app_name] = {
                    'in_sync': True,
                    'format': handler.get_format_name()
                }
                
        if all_in_sync:
            logger.info("All configuration files are in sync with the reference configuration")
        
        return all_in_sync, validation_results
    
    def print_report(self, sync_results, validation_results, source=None):
        """Print a detailed report of the synchronization operation."""
        # Determine overall status
        all_success = all(result.get('success', False) for result in sync_results.values())
        all_in_sync = all(result.get('in_sync', False) for result in validation_results.values())
        
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        overall_status = "SUCCESS" if all_success and all_in_sync else "PARTIAL_SUCCESS" if all_success else "FAILED"
        
        # Count successful configurations
        success_count = sum(1 for result in sync_results.values() if result.get('success', False))
        total_count = len(sync_results)
        
        # Determine status color and icon
        if overall_status == "SUCCESS":
            status_color = "green"
            status_icon = "✅"
        elif overall_status == "PARTIAL_SUCCESS":
            status_color = "yellow"
            status_icon = "⚠️"
        else:
            status_color = "red"
            status_icon = "❌"
        
        # Create header panel
        header_text = f"""[bold white]MCP Configuration Synchronization Report[/bold white]
[dim]{timestamp}[/dim]

{status_icon} Status: [{status_color}]{overall_status}[/{status_color}]"""
        
        if source:
            header_text += f"\n📁 Source: [cyan]{source}[/cyan]"
        
        header_text += f"""
📊 Apps Configured: [bold]{success_count}/{total_count}[/bold]"""
        
        console.print()
        console.print(Panel(header_text, title="🔄 Sync Report", border_style=status_color, padding=(1, 2)))
        
        # Create details table
        table = Table(
            title="📋 Application Details",
            box=box.ROUNDED,
            show_header=True,
            header_style="bold blue"
        )
        table.add_column("App", style="cyan", no_wrap=True, width=15)
        table.add_column("Status", justify="center", width=8)
        table.add_column("Action", style="white", width=10)
        table.add_column("Size", justify="right", width=8)
        table.add_column("Validation", justify="center", width=12)
        table.add_column("Details", style="dim", width=30)
        
        # Populate table with app details
        for app_name, result in sync_results.items():
            success = result.get('success', False)
            
            # Status icon and color
            if success:
                status_icon = "✅"
                status_color = "green"
            else:
                status_icon = "❌"
                status_color = "red"
            
            # Action and size
            action = result.get('action', 'failed') if success else result.get('action', 'failed')
            size_str = f"{result.get('size', 0)} B" if success and 'size' in result else "—"
            
            # Details column (initialize first)
            details = ""
            if success and 'path' in result:
                path_parts = str(result['path']).split('/')
                if len(path_parts) > 3:
                    details = f".../{'/'.join(path_parts[-2:])}"
                else:
                    details = str(result['path'])
            elif not success:
                if action == 'cancelled':
                    details = result.get('reason', 'user cancelled')
                else:
                    details = result.get('error', 'Unknown error')[:30] + "..." if len(result.get('error', '')) > 30 else result.get('error', 'Unknown error')
            
            # Validation status
            validation = validation_results.get(app_name, {})
            if validation and success:
                in_sync = validation.get('in_sync', False)
                if in_sync:
                    validation_display = "[green]✓ in sync[/green]"
                else:
                    reason = validation.get('reason', 'unknown')
                    mismatched_keys = validation.get('mismatched_keys', [])
                    if mismatched_keys:
                        validation_display = f"[red]✗ {reason}[/red]"
                        # Show first mismatched key in details if there are mismatches
                        details = f"Mismatch: {mismatched_keys[0]}"
                    else:
                        validation_display = f"[red]✗ {reason}[/red]"
            else:
                validation_display = "—"
            
            table.add_row(
                f"📱 {app_name}",
                f"[{status_color}]{status_icon}[/{status_color}]",
                action,
                size_str,
                validation_display,
                details
            )
        
        console.print(table)
        console.print()
        return overall_status
    
    def sync_from_file(self, app_name_or_path, force=False):
        """Synchronize MCP configuration from a specified source file."""
        # Determine source file path
        source_path = None
        source_name = None
        
        if app_name_or_path in self.CONFIG_FILES:
            source_name = app_name_or_path
            source_path = self.CONFIG_FILES[app_name_or_path]
        else:
            # Treat as direct file path
            source_path = Path(app_name_or_path)
            source_name = str(source_path)
        
        if not source_path.exists():
            logger.error(f"Source file does not exist: {source_path}")
            return False
        
        # Load configuration from source
        source_config = self.load_existing_config(source_path)
        if source_config is None:
            logger.error(f"Failed to parse source configuration at {source_path}")
            return False
        
        # Detect format and extract MCP configuration using appropriate handler
        handler = self.detect_config_format(source_config)
        mcp_config = handler.extract_mcp_config(source_config)
        
        if not mcp_config:
            logger.error(f"No MCP configuration found in {source_path}")
            return False
        
        logger.info(f"Loaded reference MCP configuration from {source_name} using {handler.get_format_name()} format")
        
        # Update config with the loaded MCP configuration
        self.config = mcp_config
        
        # Apply to all configs
        sync_results = self.update_configs(force=force)
        
        # Validate configs
        all_in_sync, validation_results = self.validate_configs()
        
        # Generate report
        status = self.print_report(sync_results, validation_results, source=source_name)
        
        if status == "SUCCESS":
            logger.info(f"MCP configuration synchronization from source completed successfully")
            return True
        else:
            logger.error(f"MCP configuration synchronization from source completed with issues")
            return False

