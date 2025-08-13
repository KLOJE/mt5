#!/usr/bin/env python3
"""
MT5 Trading Copier - Advanced Multi-Account Copy Trading System
Supports pending orders, market orders, TP/SL sync, position management
"""

import MetaTrader5 as mt5
import time
import json
import logging
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from enum import Enum
import os
import sys

class OrderType(Enum):
    BUY = 0
    SELL = 1
    BUY_LIMIT = 2
    SELL_LIMIT = 3
    BUY_STOP = 4
    SELL_STOP = 5
    BUY_STOP_LIMIT = 6
    SELL_STOP_LIMIT = 7

class OrderState(Enum):
    STARTED = 0
    PLACED = 1
    CANCELED = 2
    PARTIAL = 3
    FILLED = 4
    REJECTED = 5
    EXPIRED = 6
    REQUEST_ADD = 7
    REQUEST_MODIFY = 8
    REQUEST_CANCEL = 9

@dataclass
class OrderInfo:
    ticket: int
    symbol: str
    order_type: int
    volume: float
    price: float
    sl: float
    tp: float
    comment: str
    magic: int
    time_setup: int
    state: int
    master_ticket: Optional[int] = None
    
class LotCalculationType(Enum):
    FIXED = "fixed"
    MULTIPLIER = "multiplier" 
    PERCENT_BALANCE = "percent_balance"
    RISK_BASED = "risk_based"

@dataclass
class AccountConfig:
    login: int
    password: str
    server: str
    mt5_path: str
    lot_calculation: LotCalculationType
    lot_value: float  # Fixed lot, multiplier, percent, or risk percent
    max_lot: float
    min_lot: float
    symbol_mapping: Dict[str, str]
    magic_number: int
    enabled: bool = True
    max_spread: float = 5.0
    slippage: int = 3

class MT5Copier:
    def __init__(self, config_file: str):
        self.config = self._load_config(config_file)
        self.master_account = self.config['master']
        self.follower_accounts = self.config['followers']
        
        # SMART tracking system - only syncs when master changes
        self.master_orders: Dict[int, OrderInfo] = {}  # Current master orders
        self.follower_orders: Dict[int, Dict[int, int]] = {}  # master_ticket -> {follower_login -> follower_ticket}
        self.last_master_state: Dict[int, OrderInfo] = {}  # Previous master state for change detection
        self.order_sync_status: Dict[int, Dict[int, str]] = {}  # master_ticket -> {follower_login -> status}
        self.manually_closed: Dict[int, set] = {}  # master_ticket -> {follower_logins that manually closed}
        
        # Connection tracking
        self.connections: Dict[int, bool] = {}
        self.running = False
        
        # Performance tracking
        self.cycle_count = 0
        self.last_sync_time = {}  # follower_login -> timestamp
        
        self._setup_logging()
        self.logger.info("[INIT] Enhanced tracking system initialized")
        self.logger.info(f"[INIT] Configured for {len(self.follower_accounts)} followers")
        
    def _setup_logging(self):
        """Setup detailed logging"""
        log_format = '%(asctime)s - %(levelname)s - [%(name)s] - %(message)s'
        
        # Create console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_formatter = logging.Formatter(log_format)
        console_handler.setFormatter(console_formatter)
        
        # Create file handler with UTF-8 encoding
        file_handler = logging.FileHandler(
            f'mt5_copier_{datetime.now().strftime("%Y%m%d")}.log',
            encoding='utf-8'
        )
        file_handler.setLevel(logging.INFO)
        file_formatter = logging.Formatter(log_format)
        file_handler.setFormatter(file_formatter)
        
        # Configure logger
        self.logger = logging.getLogger('MT5Copier')
        self.logger.setLevel(logging.INFO)
        
        # Clear existing handlers
        self.logger.handlers.clear()
        
        self.logger.addHandler(console_handler)
        self.logger.addHandler(file_handler)
        
        # Prevent duplicate logs
        self.logger.propagate = False
        
        self.logger.info("=" * 80)
        self.logger.info("MT5 COPIER SYSTEM INITIALIZED")
        self.logger.info("=" * 80)
        
    def _load_config(self, config_file: str) -> dict:
        """Load configuration from JSON file"""
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
            return config
        except Exception as e:
            print(f"Error loading config: {e}")
            sys.exit(1)
    
    def connect_account(self, account_config: dict) -> bool:
        """Connect to MT5 account with detailed logging"""
        login = account_config['login']
        self.logger.info(f"[CONNECT] Attempting connection to account {login}")
        self.logger.info(f"   Server: {account_config['server']}")
        self.logger.info(f"   MT5 Path: {account_config['mt5_path']}")
        
        try:
            # Check if MT5 path exists
            if not os.path.exists(account_config['mt5_path']):
                self.logger.error(f"[ERROR] MT5 path not found: {account_config['mt5_path']}")
                return False
            
            # Initialize MT5 with specific path
            if not mt5.initialize(path=account_config['mt5_path']):
                error = mt5.last_error()
                self.logger.error(f"[ERROR] Failed to initialize MT5 for {login}: {error}")
                return False
            
            # Login to account
            if not mt5.login(login, account_config['password'], account_config['server']):
                error = mt5.last_error()
                self.logger.error(f"[ERROR] Failed to login to {login}: {error}")
                mt5.shutdown()
                return False
            
            # Get account info
            account_info = mt5.account_info()
            if account_info is None:
                self.logger.error(f"[ERROR] Failed to get account info for {login}")
                return False
            
            self.connections[login] = True
            self.logger.info(f"[SUCCESS] Successfully connected to {login}")
            self.logger.info(f"   Balance: ${account_info.balance:.2f}")
            self.logger.info(f"   Equity: ${account_info.equity:.2f}")
            self.logger.info(f"   Margin: ${account_info.margin:.2f}")
            
            return True
            
        except Exception as e:
            self.logger.error(f"[ERROR] Exception connecting to {login}: {e}")
            return False
    
    def disconnect_account(self, login: int):
        """Disconnect from MT5 account"""
        try:
            mt5.shutdown()
            self.connections[login] = False
            self.logger.info(f"[DISCONNECT] Disconnected from account {login}")
        except Exception as e:
            self.logger.error(f"[ERROR] Error disconnecting from {login}: {e}")
    
    def get_symbol_info(self, symbol: str, login: int) -> Optional[dict]:
        """Get symbol information with error handling"""
        try:
            symbol_info = mt5.symbol_info(symbol)
            if symbol_info is None:
                self.logger.warning(f"[WARNING] Symbol {symbol} not found for account {login}")
                return None
            
            if not symbol_info.visible:
                if not mt5.symbol_select(symbol, True):
                    self.logger.warning(f"[WARNING] Failed to select symbol {symbol} for account {login}")
                    return None
            
            return symbol_info._asdict()
        except Exception as e:
            self.logger.error(f"[ERROR] Error getting symbol info for {symbol} on {login}: {e}")
            return None
    
    def calculate_lot_size(self, master_volume: float, follower_config: dict, symbol: str) -> float:
        """Calculate lot size based on configuration"""
        try:
            calc_type = LotCalculationType(follower_config['lot_calculation'])
            lot_value = follower_config['lot_value']
            
            self.logger.debug(f"[CALC] Calculating lot size - Master: {master_volume}, Type: {calc_type.value}, Value: {lot_value}")
            
            if calc_type == LotCalculationType.FIXED:
                calculated_lot = lot_value
                
            elif calc_type == LotCalculationType.MULTIPLIER:
                calculated_lot = master_volume * lot_value
                
            elif calc_type == LotCalculationType.PERCENT_BALANCE:
                account_info = mt5.account_info()
                if account_info is None:
                    self.logger.error("[ERROR] Failed to get account info for lot calculation")
                    return master_volume
                
                balance = account_info.balance
                symbol_info = self.get_symbol_info(symbol, follower_config['login'])
                if symbol_info is None:
                    return master_volume
                
                contract_size = symbol_info['trade_contract_size']
                tick_value = symbol_info['trade_tick_value']
                
                risk_amount = balance * (lot_value / 100)
                calculated_lot = risk_amount / (contract_size * tick_value)
                
            elif calc_type == LotCalculationType.RISK_BASED:
                # Risk-based calculation would need SL distance
                # For now, use multiplier as fallback
                calculated_lot = master_volume * lot_value
                self.logger.warning("[WARNING] Risk-based calculation not fully implemented, using multiplier")
            
            # Apply min/max limits
            min_lot = follower_config.get('min_lot', 0.01)
            max_lot = follower_config.get('max_lot', 100.0)
            
            calculated_lot = max(min_lot, min(max_lot, calculated_lot))
            
            # Round to valid lot size
            symbol_info = self.get_symbol_info(symbol, follower_config['login'])
            if symbol_info:
                lot_step = symbol_info['volume_step']
                calculated_lot = round(calculated_lot / lot_step) * lot_step
            
            self.logger.info(f"[CALC] Calculated lot size: {calculated_lot} (from {master_volume})")
            return calculated_lot
            
        except Exception as e:
            self.logger.error(f"[ERROR] Error calculating lot size: {e}")
            return master_volume 
   
    def map_symbol(self, master_symbol: str, follower_config: dict) -> str:
        """Map symbol from master to follower account"""
        symbol_mapping = follower_config.get('symbol_mapping', {})
        mapped_symbol = symbol_mapping.get(master_symbol, master_symbol)
        
        if mapped_symbol != master_symbol:
            self.logger.info(f"[MAPPING] Symbol mapping: {master_symbol} -> {mapped_symbol}")
        
        return mapped_symbol
    
    def get_filling_mode(self, symbol: str) -> int:
        """Determine appropriate filling mode for symbol"""
        try:
            symbol_info = mt5.symbol_info(symbol)
            if symbol_info is None:
                return mt5.ORDER_FILLING_FOK
            
            filling = symbol_info.filling_mode
            
            if filling & mt5.SYMBOL_FILLING_FOK:
                self.logger.debug(f"[FILLING] Using FOK filling for {symbol}")
                return mt5.ORDER_FILLING_FOK
            elif filling & mt5.SYMBOL_FILLING_IOC:
                self.logger.debug(f"[FILLING] Using IOC filling for {symbol}")
                return mt5.ORDER_FILLING_IOC
            else:
                self.logger.debug(f"[FILLING] Using Return filling for {symbol}")
                return mt5.ORDER_FILLING_RETURN
                
        except Exception as e:
            self.logger.error(f"[ERROR] Error determining filling mode for {symbol}: {e}")
            return mt5.ORDER_FILLING_FOK
    
    def send_order(self, symbol: str, order_type: int, volume: float, price: float = 0.0, 
                   sl: float = 0.0, tp: float = 0.0, comment: str = "", magic: int = 0) -> Optional[int]:
        """Send order with comprehensive error handling"""
        try:
            self.logger.info(f"[ORDER] Sending order: {symbol} {OrderType(order_type).name} {volume} lots")
            self.logger.info(f"   Price: {price}, SL: {sl}, TP: {tp}")
            
            # Get current prices for market orders
            if order_type in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL]:
                tick = mt5.symbol_info_tick(symbol)
                if tick is None:
                    self.logger.error(f"[ERROR] Failed to get tick data for {symbol}")
                    return None
                
                price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
                self.logger.debug(f"[PRICE] Using market price: {price}")
            
            # Check spread
            symbol_info = self.get_symbol_info(symbol, 0)  # Current account
            if symbol_info:
                spread = symbol_info['spread']
                self.logger.debug(f"[SPREAD] Current spread: {spread} points")
            
            # Prepare request
            request = {
                "action": mt5.TRADE_ACTION_DEAL if order_type in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL] else mt5.TRADE_ACTION_PENDING,
                "symbol": symbol,
                "volume": volume,
                "type": order_type,
                "price": price,
                "sl": sl,
                "tp": tp,
                "comment": comment,
                "magic": magic,
                "type_filling": self.get_filling_mode(symbol),
            }
            
            # Add deviation for market orders
            if order_type in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL]:
                request["deviation"] = 20
            
            self.logger.debug(f"[REQUEST] Order request: {request}")
            
            # Send order
            result = mt5.order_send(request)
            
            if result is None:
                error = mt5.last_error()
                self.logger.error(f"[ERROR] Order send failed: {error}")
                return None
            
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                self.logger.error(f"[ERROR] Order rejected: {result.retcode} - {result.comment}")
                return None
            
            self.logger.info(f"[SUCCESS] Order executed successfully!")
            self.logger.info(f"   Ticket: {result.order}")
            self.logger.info(f"   Price: {result.price}")
            self.logger.info(f"   Volume: {result.volume}")
            
            return result.order
            
        except Exception as e:
            self.logger.error(f"[ERROR] Exception sending order: {e}")
            return None
    
    def modify_order(self, ticket: int, price: float = None, sl: float = None, tp: float = None) -> bool:
        """Modify existing order or position"""
        try:
            self.logger.info(f"[MODIFY] Modifying order/position {ticket}")
            
            # Check if it's a position or pending order
            position = mt5.positions_get(ticket=ticket)
            if position:
                # It's a position, modify SL/TP
                position = position[0]
                request = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "symbol": position.symbol,
                    "position": ticket,
                    "sl": sl if sl is not None else position.sl,
                    "tp": tp if tp is not None else position.tp,
                }
            else:
                # It's a pending order
                order = mt5.orders_get(ticket=ticket)
                if not order:
                    self.logger.error(f"[ERROR] Order {ticket} not found")
                    return False
                
                order = order[0]
                request = {
                    "action": mt5.TRADE_ACTION_MODIFY,
                    "order": ticket,
                    "price": price if price is not None else order.price_open,
                    "sl": sl if sl is not None else order.sl,
                    "tp": tp if tp is not None else order.tp,
                }
            
            self.logger.debug(f"[REQUEST] Modify request: {request}")
            
            result = mt5.order_send(request)
            
            if result is None:
                error = mt5.last_error()
                self.logger.error(f"[ERROR] Modify failed: {error}")
                return False
            
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                self.logger.error(f"[ERROR] Modify rejected: {result.retcode} - {result.comment}")
                return False
            
            self.logger.info(f"[SUCCESS] Order/Position {ticket} modified successfully")
            return True
            
        except Exception as e:
            self.logger.error(f"[ERROR] Exception modifying order {ticket}: {e}")
            return False
    
    def close_position(self, ticket: int) -> bool:
        """Close position"""
        try:
            self.logger.info(f"[CLOSE] Closing position {ticket}")
            
            position = mt5.positions_get(ticket=ticket)
            if not position:
                self.logger.error(f"[ERROR] Position {ticket} not found")
                return False
            
            position = position[0]
            
            # Determine close type
            close_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
            
            # Get current price
            tick = mt5.symbol_info_tick(position.symbol)
            if tick is None:
                self.logger.error(f"[ERROR] Failed to get tick data for {position.symbol}")
                return False
            
            close_price = tick.bid if position.type == mt5.ORDER_TYPE_BUY else tick.ask
            
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": position.symbol,
                "volume": position.volume,
                "type": close_type,
                "position": ticket,
                "price": close_price,
                "deviation": 20,
                "magic": position.magic,
                "comment": f"Close by copier",
                "type_filling": self.get_filling_mode(position.symbol),
            }
            
            self.logger.debug(f"[REQUEST] Close request: {request}")
            
            result = mt5.order_send(request)
            
            if result is None:
                error = mt5.last_error()
                self.logger.error(f"[ERROR] Close failed: {error}")
                return False
            
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                self.logger.error(f"[ERROR] Close rejected: {result.retcode} - {result.comment}")
                return False
            
            self.logger.info(f"[SUCCESS] Position {ticket} closed successfully")
            return True
            
        except Exception as e:
            self.logger.error(f"[ERROR] Exception closing position {ticket}: {e}")
            return False
    
    def cancel_order(self, ticket: int) -> bool:
        """Cancel pending order"""
        try:
            self.logger.info(f"[CANCEL] Canceling order {ticket}")
            
            request = {
                "action": mt5.TRADE_ACTION_REMOVE,
                "order": ticket,
            }
            
            result = mt5.order_send(request)
            
            if result is None:
                error = mt5.last_error()
                self.logger.error(f"[ERROR] Cancel failed: {error}")
                return False
            
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                self.logger.error(f"[ERROR] Cancel rejected: {result.retcode} - {result.comment}")
                return False
            
            self.logger.info(f"[SUCCESS] Order {ticket} canceled successfully")
            return True
            
        except Exception as e:
            self.logger.error(f"[ERROR] Exception canceling order {ticket}: {e}")
            return False
    
    def get_master_orders(self) -> List[OrderInfo]:
        """Get all orders from master account"""
        try:
            if not self.connect_account(self.master_account):
                return []
            
            orders = []
            
            # Get pending orders
            pending_orders = mt5.orders_get()
            if pending_orders:
                for order in pending_orders:
                    order_info = OrderInfo(
                        ticket=order.ticket,
                        symbol=order.symbol,
                        order_type=order.type,
                        volume=order.volume_initial,
                        price=order.price_open,
                        sl=order.sl,
                        tp=order.tp,
                        comment=order.comment,
                        magic=order.magic,
                        time_setup=order.time_setup,
                        state=order.state
                    )
                    orders.append(order_info)
            
            # Get positions (market orders)
            positions = mt5.positions_get()
            if positions:
                for pos in positions:
                    order_info = OrderInfo(
                        ticket=pos.ticket,
                        symbol=pos.symbol,
                        order_type=pos.type,
                        volume=pos.volume,
                        price=pos.price_open,
                        sl=pos.sl,
                        tp=pos.tp,
                        comment=pos.comment,
                        magic=pos.magic,
                        time_setup=pos.time,
                        state=OrderState.FILLED.value
                    )
                    orders.append(order_info)
            
            self.logger.debug(f"[DATA] Retrieved {len(orders)} orders from master account")
            return orders
            
        except Exception as e:
            self.logger.error(f"[ERROR] Error getting master orders: {e}")
            return []
    
    def sync_orders_to_follower(self, follower_config: dict, master_orders: List[OrderInfo]):
        """Sync orders to a specific follower account"""
        try:
            login = follower_config['login']
            self.logger.info(f"[SYNC] Syncing orders to follower {login}")
            
            if not self.connect_account(follower_config):
                return
            
            # Get current follower orders
            follower_orders = {}
            magic = follower_config['magic_number']
            
            # Get pending orders (only with our magic number)
            pending = mt5.orders_get()
            if pending:
                for order in pending:
                    if order.magic == magic:  # Only track our orders
                        follower_orders[order.ticket] = order
                        self.logger.debug(f"[SYNC] Found pending order {order.ticket} for {order.symbol}")
            
            # Get positions (only with our magic number)
            positions = mt5.positions_get()
            if positions:
                for pos in positions:
                    if pos.magic == magic:  # Only track our positions
                        follower_orders[pos.ticket] = pos
                        self.logger.debug(f"[SYNC] Found position {pos.ticket} for {pos.symbol}")
            
            self.logger.info(f"[SYNC] Found {len(follower_orders)} existing orders/positions for follower {login}")
            
            # Process each master order
            for master_order in master_orders:
                self._process_master_order(master_order, follower_config, follower_orders)
            
            # IMPORTANT: Check for orders to close/cancel (exist in follower but not in master)
            self._cleanup_orphaned_orders(master_orders, follower_orders, follower_config)
            
        except Exception as e:
            self.logger.error(f"[ERROR] Error syncing to follower {follower_config['login']}: {e}")
    
    def _process_master_order(self, master_order: OrderInfo, follower_config: dict, follower_orders: dict):
        """Process a single master order for copying - ENHANCED WITH SMART STATE TRACKING"""
        try:
            login = follower_config['login']
            master_ticket = master_order.ticket
            
            # Check if we already have this order copied
            if master_ticket in self.follower_orders:
                if login in self.follower_orders[master_ticket]:
                    follower_ticket = self.follower_orders[master_ticket][login]
                    
                    # Check if follower order still exists
                    if follower_ticket in follower_orders:
                        # Order exists - check if master order actually changed
                        if self._has_master_order_changed(master_order):
                            self.logger.debug(f"[SMART] Master order {master_ticket} changed, checking modifications")
                            self._check_order_modifications(master_order, follower_orders[follower_ticket], follower_config)
                        else:
                            self.logger.debug(f"[SMART] Master order {master_ticket} unchanged, skipping modification check")
                        
                        # Update sync status
                        if master_ticket not in self.order_sync_status:
                            self.order_sync_status[master_ticket] = {}
                        self.order_sync_status[master_ticket][login] = "synced"
                        return
                    else:
                        # Follower order was manually closed - mark it and DON'T re-copy
                        self.logger.info(f"[MANUAL] Follower {login} manually closed order {master_ticket}, marking as manually closed")
                        
                        # Track manual closure
                        if master_ticket not in self.manually_closed:
                            self.manually_closed[master_ticket] = set()
                        self.manually_closed[master_ticket].add(login)
                        
                        # Remove from tracking
                        del self.follower_orders[master_ticket][login]
                        if master_ticket in self.order_sync_status and login in self.order_sync_status[master_ticket]:
                            del self.order_sync_status[master_ticket][login]
                        if not self.follower_orders[master_ticket]:
                            del self.follower_orders[master_ticket]
                            if master_ticket in self.order_sync_status:
                                del self.order_sync_status[master_ticket]
                        return  # DON'T try to re-copy
            
            # Check if this follower manually closed this order before
            if master_ticket in self.manually_closed and login in self.manually_closed[master_ticket]:
                self.logger.debug(f"[SKIP] Follower {login} manually closed order {master_ticket} before, not re-copying")
                return
            
            # New order to copy
            self.logger.info(f"[NEW] New master order {master_ticket} detected, copying to follower {login}")
            self._copy_new_order(master_order, follower_config)
            
        except Exception as e:
            self.logger.error(f"[ERROR] Error processing master order {master_order.ticket}: {e}")
    
    def _has_master_order_changed(self, master_order: OrderInfo) -> bool:
        """Check if master order has changed since last cycle - SMART CHANGE DETECTION"""
        try:
            master_ticket = master_order.ticket
            
            # If we don't have previous state, consider it changed
            if master_ticket not in self.last_master_state:
                return True
            
            last_order = self.last_master_state[master_ticket]
            
            # Compare relevant fields that would require follower updates
            changes = []
            
            # Price changes (for pending orders)
            if abs(master_order.price - last_order.price) > 0.00001:
                changes.append("price")
            
            # SL changes
            master_sl = master_order.sl if master_order.sl else 0.0
            last_sl = last_order.sl if last_order.sl else 0.0
            if abs(master_sl - last_sl) > 0.00001:
                changes.append("sl")
            
            # TP changes
            master_tp = master_order.tp if master_order.tp else 0.0
            last_tp = last_order.tp if last_order.tp else 0.0
            if abs(master_tp - last_tp) > 0.00001:
                changes.append("tp")
            
            # Volume changes (shouldn't happen but let's track it)
            if abs(master_order.volume - last_order.volume) > 0.00001:
                changes.append("volume")
            
            if changes:
                self.logger.debug(f"[CHANGE] Master order {master_ticket} changed: {', '.join(changes)}")
                return True
            
            return False
            
        except Exception as e:
            self.logger.error(f"[ERROR] Error checking master order changes for {master_order.ticket}: {e}")
            return True  # If error, assume changed to be safe
    
    def _copy_new_order(self, master_order: OrderInfo, follower_config: dict):
        """Copy a new order to follower account"""
        try:
            login = follower_config['login']
            
            # Map symbol
            symbol = self.map_symbol(master_order.symbol, follower_config)
            
            # Check if symbol exists
            if not self.get_symbol_info(symbol, login):
                self.logger.warning(f"[WARNING] Symbol {symbol} not available for follower {login}, skipping")
                return
            
            # Calculate lot size
            volume = self.calculate_lot_size(master_order.volume, follower_config, symbol)
            
            # Copy the order
            comment = f"Copy:{master_order.ticket}"
            magic = follower_config['magic_number']
            
            follower_ticket = self.send_order(
                symbol=symbol,
                order_type=master_order.order_type,
                volume=volume,
                price=master_order.price,
                sl=master_order.sl,
                tp=master_order.tp,
                comment=comment,
                magic=magic
            )
            
            if follower_ticket:
                # Track the copied order
                if master_order.ticket not in self.follower_orders:
                    self.follower_orders[master_order.ticket] = {}
                
                self.follower_orders[master_order.ticket][login] = follower_ticket
                
                self.logger.info(f"[SUCCESS] Copied order {master_order.ticket} -> {follower_ticket} for {login}")
            
        except Exception as e:
            self.logger.error(f"[ERROR] Error copying order {master_order.ticket} to {follower_config['login']}: {e}")
    
    def _check_order_modifications(self, master_order: OrderInfo, follower_order, follower_config: dict):
        """Check and sync order modifications - SMART LOGIC"""
        try:
            modifications_needed = []
            
            # Check SL/TP changes with proper null handling
            master_sl = master_order.sl if master_order.sl else 0.0
            master_tp = master_order.tp if master_order.tp else 0.0
            follower_sl = follower_order.sl if follower_order.sl else 0.0
            follower_tp = follower_order.tp if follower_order.tp else 0.0
            
            sl_changed = abs(master_sl - follower_sl) > 0.00001
            tp_changed = abs(master_tp - follower_tp) > 0.00001
            
            if sl_changed:
                modifications_needed.append(f"SL: {follower_sl} -> {master_sl}")
            if tp_changed:
                modifications_needed.append(f"TP: {follower_tp} -> {master_tp}")
            
            # For pending orders, check price changes
            price_changed = False
            if hasattr(follower_order, 'price_open'):
                master_price = master_order.price if master_order.price else 0.0
                follower_price = follower_order.price_open if follower_order.price_open else 0.0
                price_changed = abs(master_price - follower_price) > 0.00001
                if price_changed:
                    modifications_needed.append(f"Price: {follower_price} -> {master_price}")
            
            # Only modify if there are actual changes
            if modifications_needed:
                self.logger.info(f"[MODIFY] Changes detected for {master_order.ticket}: {', '.join(modifications_needed)}")
                
                # Prepare modification parameters
                modify_params = {}
                if sl_changed:
                    modify_params['sl'] = master_sl
                if tp_changed:
                    modify_params['tp'] = master_tp
                if price_changed:
                    modify_params['price'] = master_order.price
                
                success = self.modify_order(ticket=follower_order.ticket, **modify_params)
                if success:
                    self.logger.info(f"[SUCCESS] Successfully updated follower order {follower_order.ticket}")
                else:
                    self.logger.warning(f"[WARNING] Failed to update follower order {follower_order.ticket}")
            else:
                # No changes needed - don't log this every cycle to reduce noise
                self.logger.debug(f"[SKIP] No changes needed for {master_order.ticket}")
            
        except Exception as e:
            self.logger.error(f"[ERROR] Error checking modifications for {master_order.ticket}: {e}")
    
    def _cleanup_orphaned_orders(self, master_orders: List[OrderInfo], follower_orders: dict, follower_config: dict):
        """Close/cancel orders that exist in follower but not in master"""
        try:
            login = follower_config['login']
            magic = follower_config['magic_number']
            
            master_tickets = {order.ticket for order in master_orders}
            self.logger.debug(f"[CLEANUP] Master tickets: {master_tickets}")
            self.logger.debug(f"[CLEANUP] Checking {len(follower_orders)} follower orders")
            
            # Method 1: Check follower orders with our magic number
            orders_to_close = []
            for ticket, order in follower_orders.items():
                # Only process orders with our magic number
                if order.magic != magic:
                    continue
                
                # Extract master ticket from comment
                if hasattr(order, 'comment') and order.comment and order.comment.startswith('Copy:'):
                    try:
                        master_ticket = int(order.comment.split(':')[1])
                        if master_ticket not in master_tickets:
                            orders_to_close.append((ticket, order, master_ticket))
                    except (ValueError, IndexError):
                        continue
            
            # Method 2: Check our internal tracking for missing master orders
            for master_ticket, followers in list(self.follower_orders.items()):
                if master_ticket not in master_tickets:
                    if login in followers:
                        follower_ticket = followers[login]
                        # Find the order in follower_orders
                        if follower_ticket in follower_orders:
                            order = follower_orders[follower_ticket]
                            orders_to_close.append((follower_ticket, order, master_ticket))
            
            # Close/cancel the identified orders
            for ticket, order, master_ticket in orders_to_close:
                self.logger.info(f"[CLEANUP] Master order {master_ticket} no longer exists, closing follower {ticket}")
                
                success = False
                # Check if it's a position or pending order
                if hasattr(order, 'type'):
                    if order.type in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL]:
                        # It's a position
                        success = self.close_position(ticket)
                    else:
                        # It's a pending order
                        success = self.cancel_order(ticket)
                else:
                    # Try to determine from positions/orders
                    position = mt5.positions_get(ticket=ticket)
                    if position:
                        success = self.close_position(ticket)
                    else:
                        success = self.cancel_order(ticket)
                
                # Remove from tracking if successful
                if success and master_ticket in self.follower_orders:
                    if login in self.follower_orders[master_ticket]:
                        del self.follower_orders[master_ticket][login]
                        self.logger.debug(f"[CLEANUP] Removed {master_ticket}->{login} from tracking")
                    if not self.follower_orders[master_ticket]:
                        del self.follower_orders[master_ticket]
                        self.logger.debug(f"[CLEANUP] Removed master ticket {master_ticket} from tracking")
            
        except Exception as e:
            self.logger.error(f"[ERROR] Error cleaning up orphaned orders for {follower_config['login']}: {e}")
    
    def run_copy_cycle(self):
        """SMART copy cycle - only sync to followers when master changes detected"""
        try:
            self.cycle_count += 1
            self.logger.info(f"[CYCLE] Starting smart cycle #{self.cycle_count}")
            
            # Get master orders
            master_orders = self.get_master_orders()
            current_master_state = {order.ticket: order for order in master_orders}
            
            # Detect changes in master account
            changes_detected = self._detect_master_changes(current_master_state, master_orders)
            
            if not changes_detected['has_changes']:
                self.logger.debug("[SMART] No master changes detected, skipping follower sync")
                # Update state and return - NO follower checking
                self.last_master_state = current_master_state.copy()
                return
            
            # Changes detected - log them
            self.logger.info(f"[CHANGES] Master changes detected: {changes_detected['summary']}")
            
            if not master_orders:
                self.logger.debug("[DATA] No orders found in master account")
                # Still need to check for cleanup if orders were removed
                for follower_config in self.follower_accounts:
                    if follower_config.get('enabled', True):
                        self.sync_orders_to_follower(follower_config, [])
            else:
                self.logger.info(f"[DATA] Found {len(master_orders)} orders in master account")
                
                # Log master orders summary
                pending_count = sum(1 for order in master_orders if order.state != OrderState.FILLED.value)
                position_count = sum(1 for order in master_orders if order.state == OrderState.FILLED.value)
                
                self.logger.info(f"   Pending orders: {pending_count}")
                self.logger.info(f"   Open positions: {position_count}")
                
                # ONLY sync to followers because changes were detected
                for follower_config in self.follower_accounts:
                    if not follower_config.get('enabled', True):
                        self.logger.debug(f"[SKIP] Follower {follower_config['login']} is disabled, skipping")
                        continue
                    
                    self.logger.info(f"[SYNC] Syncing changes to follower {follower_config['login']}")
                    self.sync_orders_to_follower(follower_config, master_orders)
            
            # Update last master state for next cycle comparison
            self.last_master_state = current_master_state.copy()
            
            # Log tracking statistics every 10 cycles
            if self.cycle_count % 10 == 0:
                self._log_tracking_stats()
            
            self.logger.info(f"[SUCCESS] Smart cycle #{self.cycle_count} completed")
            
        except Exception as e:
            self.logger.error(f"[ERROR] Error in smart cycle #{self.cycle_count}: {e}")
    
    def _detect_master_changes(self, current_master_state: dict, master_orders: List[OrderInfo]) -> dict:
        """Detect if there are any changes in master account that require follower sync"""
        try:
            changes = {
                'has_changes': False,
                'summary': []
            }
            
            current_tickets = set(current_master_state.keys())
            last_tickets = set(self.last_master_state.keys())
            
            # Check for new orders
            new_orders = current_tickets - last_tickets
            if new_orders:
                changes['has_changes'] = True
                changes['summary'].append(f"{len(new_orders)} new orders")
                self.logger.debug(f"[DETECT] New orders: {list(new_orders)}")
            
            # Check for removed orders
            removed_orders = last_tickets - current_tickets
            if removed_orders:
                changes['has_changes'] = True
                changes['summary'].append(f"{len(removed_orders)} removed orders")
                self.logger.debug(f"[DETECT] Removed orders: {list(removed_orders)}")
            
            # Check for modified orders (existing orders that changed)
            modified_count = 0
            common_tickets = current_tickets & last_tickets
            for ticket in common_tickets:
                current_order = current_master_state[ticket]
                if self._has_master_order_changed(current_order):
                    modified_count += 1
                    changes['has_changes'] = True
            
            if modified_count > 0:
                changes['summary'].append(f"{modified_count} modified orders")
                self.logger.debug(f"[DETECT] {modified_count} orders modified")
            
            # If no changes detected
            if not changes['summary']:
                changes['summary'] = ['no changes']
                self.logger.debug("[DETECT] No changes in master account")
            
            return changes
            
        except Exception as e:
            self.logger.error(f"[ERROR] Error detecting master changes: {e}")
            # If error, assume changes to be safe
            return {'has_changes': True, 'summary': ['error - assuming changes']}
    
    def _log_tracking_stats(self):
        """Log tracking statistics for monitoring"""
        try:
            total_tracked = len(self.follower_orders)
            total_followers = len(self.follower_accounts)
            
            self.logger.info(f"[STATS] Tracking {total_tracked} master orders across {total_followers} followers")
            
            for master_ticket, followers in self.follower_orders.items():
                follower_count = len(followers)
                self.logger.debug(f"[STATS] Master {master_ticket} -> {follower_count} followers: {list(followers.keys())}")
            
        except Exception as e:
            self.logger.error(f"[ERROR] Error logging tracking stats: {e}")
    
    def start(self):
        """Start the copier system"""
        self.logger.info("[START] STARTING MT5 COPIER SYSTEM")
        self.logger.info(f"   Master Account: {self.master_account['login']}")
        self.logger.info(f"   Follower Accounts: {len(self.follower_accounts)}")
        
        # Test connections
        self.logger.info("[TEST] Testing connections...")
        
        if not self.connect_account(self.master_account):
            self.logger.error("[ERROR] Failed to connect to master account")
            return False
        
        for follower in self.follower_accounts:
            if follower.get('enabled', True):
                if not self.connect_account(follower):
                    self.logger.error(f"[ERROR] Failed to connect to follower {follower['login']}")
                    return False
        
        self.logger.info("[SUCCESS] All connections successful")
        
        self.running = True
        
        # Main loop
        try:
            while self.running:
                self.run_copy_cycle()
                
                # Sleep between cycles
                sleep_time = self.config.get('copy_interval', 1)
                self.logger.debug(f"[SLEEP] Sleeping for {sleep_time} seconds")
                time.sleep(sleep_time)
                
        except KeyboardInterrupt:
            self.logger.info("[STOP] Received stop signal")
        except Exception as e:
            self.logger.error(f"[ERROR] Fatal error in main loop: {e}")
        finally:
            self.stop()
        
        return True
    
    def stop(self):
        """Stop the copier system"""
        self.logger.info("[STOP] STOPPING MT5 COPIER SYSTEM")
        self.running = False
        
        # Disconnect all accounts
        for login in self.connections:
            if self.connections[login]:
                self.disconnect_account(login)
        
        self.logger.info("[SUCCESS] MT5 Copier stopped successfully")

def main():
    """Main entry point"""
    if len(sys.argv) != 2:
        print("Usage: python mt5_copier_fixed.py <config_file>")
        sys.exit(1)
    
    config_file = sys.argv[1]
    
    if not os.path.exists(config_file):
        print(f"Config file {config_file} not found")
        sys.exit(1)
    
    copier = MT5Copier(config_file)
    copier.start()

if __name__ == "__main__":
    main()