"""
Authentication service layer for handling user registration, login, and logout.

This module provides business logic for authentication operations,
separating concerns from the API endpoint handlers.
"""

import jwt
import logging
from datetime import datetime, timedelta
from functools import wraps
from flask import current_app, g, request
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.exc import IntegrityError
import uuid
from __init__ import db
from model.user import User
from model.utils.response import APIResponse, AuthError, ValidationError


logger = logging.getLogger(__name__)


class AuthService:
    """Service for authentication operations."""

    @staticmethod
    def register_user(name: str, email: str, password: str, role: str = "User") -> dict:
        """
        Register a new user.

        Args:
            name: User name
            email: User email
            password: User password (will be hashed)
            role: User role (default: "User")

        Returns:
            dict: User data

        Raises:
            ValidationError: If validation fails
        """
        # Validation
        if not name or len(name) < 2:
            raise ValidationError("Name is required and must be at least 2 characters")
        
        if not email or "@" not in email:
            raise ValidationError("Valid email is required")
        
        if not password or len(password) < 6:
            raise ValidationError("Password is required and must be at least 6 characters")
        
        if role not in ["User", "Vendor", "Admin"]:
            raise ValidationError(f"Invalid role: {role}")

        # Check for existing user
        existing_user = User.query.filter_by(_email=email).first()
        if existing_user:
            raise ValidationError(f"User with email {email} already exists")
        
        # Create user object with unique UID
        try:
            # Generate unique UID by appending uuid suffix since email prefix can collide
            # Format: email_prefix-uuid_suffix (e.g., alice-a1b2c3d4)
            email_prefix = email.split("@")[0]
            unique_suffix = str(uuid.uuid4())[:8]
            unique_uid = f"{email_prefix}-{unique_suffix}"
            
            user = User(
                name=name,
                email=email,
                uid=unique_uid,
                password=password,
                role=role,
            )
            
            db.session.add(user)
            db.session.commit()
            
            logger.info(f"User registered: {email} with UID: {unique_uid}")
            return user.read()
            
        except IntegrityError as e:
            db.session.rollback()
            # Map SQL integrity errors to specific validation errors
            error_msg = str(e.orig).lower()
            if "unique constraint" in error_msg or "duplicate" in error_msg:
                logger.warning(f"Registration failed - duplicate user data: {email}")
                raise ValidationError(f"User with email {email} already exists")
            else:
                logger.error(f"Database integrity error during registration: {error_msg}")
                raise ValidationError("A user with this information already exists")
        except Exception as e:
            db.session.rollback()
            logger.error(f"Unexpected error during user registration: {str(e)}", exc_info=True)
            # Return generic message to client - don't expose internal errors
            raise ValidationError("An error occurred during registration. Please try again.")

    @staticmethod
    def login_user(email: str, password: str) -> str:
        """
        Authenticate user and generate JWT token.
        
        Args:
            email: User email
            password: User password (plain text)
            
        Returns:
            str: JWT token
            
        Raises:
            AuthError: If authentication fails
        """
        if not email or not password:
            raise AuthError("Email and password are required", "INVALID_CREDENTIALS")
        
        user = User.query.filter_by(_email=email).first()
        if not user:
            logger.warning(f"Login attempt with non-existent email: {email}")
            raise AuthError("Invalid email or password", "INVALID_CREDENTIALS")
        
        # Check password
        if not check_password_hash(user._password, password):
            logger.warning(f"Failed login attempt for user: {email}")
            raise AuthError("Invalid email or password", "INVALID_CREDENTIALS")
        
        # Check if user is active
        if not user.is_active:
            raise AuthError("User account is inactive", "ACCOUNT_INACTIVE")
        
        # Generate JWT token
        try:
            token = AuthService.generate_jwt_token(user)
            logger.info(f"User logged in: {email}")
            return token
        except Exception as e:
            logger.error(f"Error generating JWT token: {str(e)}")
            raise AuthError("Error generating authentication token", "TOKEN_ERROR")

    @staticmethod
    def logout_user(user_id: int) -> bool:
        """
        Logout user (for audit purposes, actual logout is handled client-side with cookie deletion).
        
        Args:
            user_id: User ID
            
        Returns:
            bool: Success status
        """
        try:
            user = User.query.get(user_id)
            if user:
                logger.info(f"User logged out: {user._email}")
            return True
        except Exception as e:
            logger.error(f"Error logging out user: {str(e)}")
            return False

    @staticmethod
    def generate_jwt_token(user: User, expires_in: int = 86400) -> str:
        """
        Generate JWT token for a user.
        
        Args:
            user: User object
            expires_in: Token expiration time in seconds (default: 24 hours)
            
        Returns:
            str: JWT token
        """
        payload = {
            "id": user.id,
            "_uid": user._uid,
            "_email": user._email,
            "_name": user._name,
            "_role": user._role,
            "iat": datetime.utcnow(),
            "exp": datetime.utcnow() + timedelta(seconds=expires_in)
        }
        
        token = jwt.encode(
            payload,
            current_app.config["SECRET_KEY"],
            algorithm="HS256"
        )
        
        return token

    @staticmethod
    def verify_jwt_token(token: str) -> dict:
        """
        Verify and decode JWT token.
        
        Args:
            token: JWT token
            
        Returns:
            dict: Decoded token data
            
        Raises:
            AuthError: If token is invalid or expired
        """
        try:
            payload = jwt.decode(
                token,
                current_app.config["SECRET_KEY"],
                algorithms=["HS256"]
            )
            return payload
        except jwt.ExpiredSignatureError:
            raise AuthError("Token has expired", "TOKEN_EXPIRED")
        except jwt.InvalidTokenError:
            raise AuthError("Invalid token", "INVALID_TOKEN")

    @staticmethod
    def get_current_user():
        """
        Get the current authenticated user from g object.
        
        Returns:
            User: Current user object or None
        """
        return g.get("current_user")


def token_required(roles=None):
    """
    Decorator to guard API endpoints that require authentication.
    
    Supports two authentication methods:
    1. Authorization header (preferred for non-browser clients): Authorization: Bearer <token>
    2. Cookie (for browser-based clients): JWT_TOKEN_NAME cookie
    
    Args:
        roles: List of allowed roles (optional). If provided, user must have one of these roles.
        
    Returns:
        function: Decorated function
        
    Example:
        @app.route('/api/collection')
        @token_required(roles=['User', 'Admin'])
        def add_to_collection():
            current_user = g.current_user
            ...
    """
    def decorator(func):
        @wraps(func)
        def decorated_function(*args, **kwargs):
            # Try to get token from Authorization header first (Bearer token)
            auth_header = request.headers.get('Authorization', '')
            token = None
            
            if auth_header.startswith('Bearer '):
                # Extract Bearer token
                token = auth_header[7:]  # Remove 'Bearer ' prefix
            elif not token:
                # Fall back to cookie if no Bearer token found
                token = request.cookies.get(current_app.config["JWT_TOKEN_NAME"])
            
            if not token:
                return APIResponse.unauthorized(
                    message="Authentication token is missing. Use 'Authorization: Bearer <token>' header or set JWT token cookie"
                )
            
            try:
                # Verify token
                payload = AuthService.verify_jwt_token(token)
                
                # Get user from database
                user = User.query.get(payload.get("id"))
                if not user or not user.is_active:
                    return APIResponse.unauthorized(
                        message="User not found or inactive"
                    )
                
                # Check role if specified
                if roles and user._role not in roles:
                    return APIResponse.forbidden(
                        message=f"This action requires one of these roles: {', '.join(roles)}"
                    )
                
                # Set current user in g object
                g.current_user = user
                
            except AuthError as e:
                return APIResponse.unauthorized(
                    message=e.message
                )
            except Exception as e:
                logger.error(f"Authentication error: {str(e)}")
                return APIResponse.error(
                    message="Authentication failed",
                    error_code="AUTH_ERROR",
                    status_code=401
                )
            
            return func(*args, **kwargs)
        
        return decorated_function
    return decorator


def rbac_required(*allowed_roles):
    """
    Decorator for role-based access control.
    
    Must be used after @token_required decorator.
    
    Args:
        allowed_roles: Roles that are allowed to access the endpoint
        
    Returns:
        function: Decorated function
        
    Example:
        @app.route('/api/donations/<id>', methods=['DELETE'])
        @token_required()
        @rbac_required('Admin', 'Donor')
        def delete_donation(id):
            ...
    """
    def decorator(func):
        @wraps(func)
        def decorated_function(*args, **kwargs):
            current_user = g.get("current_user")
            
            if not current_user:
                return APIResponse.unauthorized(
                    message="Authentication required"
                )
            
            if current_user._role not in allowed_roles:
                return APIResponse.forbidden(
                    message=f"This action requires one of these roles: {', '.join(allowed_roles)}"
                )
            
            return func(*args, **kwargs)
        
        return decorated_function
    return decorator


def owner_required(get_resource_owner):
    """
    Decorator for resource owner access control.
    
    Ensures user can only modify their own resources.
    
    Args:
        get_resource_owner: Function that extracts owner_id from the resource
        
    Returns:
        function: Decorated function
        
    Example:
        def get_donation_owner(donation_id):
            donation = Donation.query.get(donation_id)
            return donation.donor_id if donation else None
        
        @app.route('/api/donations/<id>', methods=['PUT'])
        @token_required()
        @owner_required(lambda id: get_donation_owner(id))
        def update_donation(id):
            ...
    """
    def decorator(func):
        @wraps(func)
        def decorated_function(*args, **kwargs):
            current_user = g.get("current_user")
            
            if not current_user:
                return APIResponse.unauthorized(
                    message="Authentication required"
                )
            
            # Admin can access any resource
            if current_user._role == "Admin":
                return func(*args, **kwargs)
            
            # Get resource owner
            resource_id = kwargs.get("id") or kwargs.get("donation_id") or kwargs.get("user_id")
            owner_id = get_resource_owner(resource_id)
            
            if not owner_id or current_user.id != owner_id:
                return APIResponse.forbidden(
                    message="You can only modify your own resources"
                )
            
            return func(*args, **kwargs)
        
        return decorated_function
    return decorator
