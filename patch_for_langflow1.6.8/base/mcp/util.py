import asyncio
import contextlib
import inspect
import os
import platform
import re
import shutil
import unicodedata
from collections.abc import Awaitable, Callable
########## MOD START ##########
# from typing import Any
from typing import Any, Dict, Union
########## MOD END ##########
from urllib.parse import urlparse
from uuid import UUID

import httpx
from anyio import ClosedResourceError
from httpx import codes as httpx_codes
from langchain_core.tools import StructuredTool
from mcp import ClientSession
from mcp.shared.exceptions import McpError
from pydantic import BaseModel, Field, create_model
from sqlmodel import select

from langflow.logging.logger import logger
from langflow.services.database.models.flow.model import Flow
from langflow.services.deps import get_settings_service

HTTP_ERROR_STATUS_CODE = httpx_codes.BAD_REQUEST  # HTTP status code for client errors
NULLABLE_TYPE_LENGTH = 2  # Number of types in a nullable union (the type itself + null)

# HTTP status codes used in validation
HTTP_NOT_FOUND = 404
HTTP_BAD_REQUEST = 400
HTTP_INTERNAL_SERVER_ERROR = 500

# MCP Session Manager constants - lazy loaded
_mcp_settings_cache: dict[str, Any] = {}


def _get_mcp_setting(key: str, default: Any = None) -> Any:
    """Lazy load MCP settings from settings service."""
    if key not in _mcp_settings_cache:
        settings = get_settings_service().settings
        _mcp_settings_cache[key] = getattr(settings, key, default)
    return _mcp_settings_cache[key]


def get_max_sessions_per_server() -> int:
    """Get maximum number of sessions per server to prevent resource exhaustion."""
    return _get_mcp_setting("mcp_max_sessions_per_server")


def get_session_idle_timeout() -> int:
    """Get 5 minutes idle timeout for sessions."""
    return _get_mcp_setting("mcp_session_idle_timeout")


def get_session_cleanup_interval() -> int:
    """Get cleanup interval in seconds."""
    return _get_mcp_setting("mcp_session_cleanup_interval")


# RFC 7230 compliant header name pattern: token = 1*tchar
# tchar = "!" / "#" / "$" / "%" / "&" / "'" / "*" / "+" / "-" / "." /
#         "^" / "_" / "`" / "|" / "~" / DIGIT / ALPHA
HEADER_NAME_PATTERN = re.compile(r"^[!#$%&\'*+\-.0-9A-Z^_`a-z|~]+$")

# Common allowed headers for MCP connections
ALLOWED_HEADERS = {
    "authorization",
    "accept",
    "accept-encoding",
    "accept-language",
    "cache-control",
    "content-type",
    "user-agent",
    "x-api-key",
    "x-auth-token",
    "x-custom-header",
    "x-langflow-session",
    "x-mcp-client",
    "x-requested-with",
}


def validate_headers(headers: dict[str, str]) -> dict[str, str]:
    """Validate and sanitize HTTP headers according to RFC 7230.

    Args:
        headers: Dictionary of header name-value pairs

    Returns:
        Dictionary of validated and sanitized headers

    Raises:
        ValueError: If headers contain invalid names or values
    """
    if not headers:
        return {}

    sanitized_headers = {}

    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            logger.warning(f"Skipping non-string header: {name}={value}")
            continue

        # Validate header name according to RFC 7230
        if not HEADER_NAME_PATTERN.match(name):
            logger.warning(f"Invalid header name '{name}', skipping")
            continue

        # Normalize header name to lowercase (HTTP headers are case-insensitive)
        normalized_name = name.lower()

        # Optional: Check against whitelist of allowed headers
        if normalized_name not in ALLOWED_HEADERS:
            # For MCP, we'll be permissive and allow non-standard headers
            # but log a warning for security awareness
            logger.debug(f"Using non-standard header: {normalized_name}")

        # Check for potential header injection attempts BEFORE sanitizing
        if "\r" in value or "\n" in value:
            logger.warning(f"Potential header injection detected in '{name}', skipping")
            continue

        # Sanitize header value - remove control characters and newlines
        # RFC 7230: field-value = *( field-content / obs-fold )
        # We'll remove control characters (0x00-0x1F, 0x7F) except tab (0x09) and space (0x20)
        sanitized_value = re.sub(r"[\x00-\x08\x0A-\x1F\x7F]", "", value)

        # Remove leading/trailing whitespace
        sanitized_value = sanitized_value.strip()

        if not sanitized_value:
            logger.warning(f"Header '{name}' has empty value after sanitization, skipping")
            continue

        sanitized_headers[normalized_name] = sanitized_value

    return sanitized_headers


def sanitize_mcp_name(name: str, max_length: int = 46) -> str:
    """Sanitize a name for MCP usage by removing emojis, diacritics, and special characters.

    Args:
        name: The original name to sanitize
        max_length: Maximum length for the sanitized name

    Returns:
        A sanitized name containing only letters, numbers, hyphens, and underscores
    """
    if not name or not name.strip():
        return ""

    # Remove emojis using regex pattern
    emoji_pattern = re.compile(
        "["
        "\U0001f600-\U0001f64f"  # emoticons
        "\U0001f300-\U0001f5ff"  # symbols & pictographs
        "\U0001f680-\U0001f6ff"  # transport & map symbols
        "\U0001f1e0-\U0001f1ff"  # flags (iOS)
        "\U00002500-\U00002bef"  # chinese char
        "\U00002702-\U000027b0"
        "\U00002702-\U000027b0"
        "\U000024c2-\U0001f251"
        "\U0001f926-\U0001f937"
        "\U00010000-\U0010ffff"
        "\u2640-\u2642"
        "\u2600-\u2b55"
        "\u200d"
        "\u23cf"
        "\u23e9"
        "\u231a"
        "\ufe0f"  # dingbats
        "\u3030"
        "]+",
        flags=re.UNICODE,
    )

    # Remove emojis
    name = emoji_pattern.sub("", name)

    # Normalize unicode characters to remove diacritics
    name = unicodedata.normalize("NFD", name)
    name = "".join(char for char in name if unicodedata.category(char) != "Mn")

    # Replace spaces and special characters with underscores
    name = re.sub(r"[^\w\s-]", "", name)  # Keep only word chars, spaces, and hyphens
    name = re.sub(r"[-\s]+", "_", name)  # Replace spaces and hyphens with underscores
    name = re.sub(r"_+", "_", name)  # Collapse multiple underscores

    # Remove leading/trailing underscores
    name = name.strip("_")

    # Ensure it starts with a letter or underscore (not a number)
    if name and name[0].isdigit():
        name = f"_{name}"

    # Convert to lowercase
    name = name.lower()

    # Truncate to max length
    if len(name) > max_length:
        name = name[:max_length].rstrip("_")

    # If empty after sanitization, provide a default
    if not name:
        name = "unnamed"

    return name

########## MOD START ##########
def _fill_defaults(arg_schema: type[BaseModel], provided_args: dict) -> None:
    """Fill default values for missing fields in the provided arguments."""
    for field, field_info in arg_schema.model_fields.items():
        if field not in provided_args:
            field_type = field_info.annotation
            field_type_str = str(field_type).lower()

            if "list" in field_type_str or str(field_type) == "list":
                provided_args[field] = []
            elif "dict" in field_type_str or str(field_type) == "dict" or "object" in field_type_str:
                provided_args[field] = {}
            elif "str" in field_type_str or str(field_type) == "str":
                provided_args[field] = ""
            elif "int" in field_type_str or str(field_type) == "int":
                provided_args[field] = 0
            elif "float" in field_type_str or str(field_type) == "float":
                provided_args[field] = 0.0
            elif "bool" in field_type_str or str(field_type) == "bool":
                provided_args[field] = False
            else:
                provided_args[field] = None

def _post_process_arguments(arg_schema: type[BaseModel], arguments: dict) -> None:
    """Post-process arguments to handle JSON parsing and type normalization."""
    import json
    from typing import get_origin, get_args, Union

    # 1. Normalize types (Union handling and basic string conversion)
    for field_name, value in arguments.items():
        field_info = arg_schema.model_fields.get(field_name)
        if field_info:
            expected_type = field_info.annotation
            if get_origin(expected_type) is Union:
                union_args = get_args(expected_type)
                if str in union_args and isinstance(value, (int, float, bool)):
                    arguments[field_name] = str(value)
                elif int in union_args and isinstance(value, str):
                    try:
                        arguments[field_name] = int(value)
                    except ValueError:
                        pass
                elif float in union_args and isinstance(value, str):
                    try:
                        arguments[field_name] = float(value)
                    except ValueError:
                        pass
                elif bool in union_args and isinstance(value, str):
                    arguments[field_name] = value.lower() in ('true', '1', 'yes', 'on')
            else:
                if expected_type == str and isinstance(value, (int, float)):
                    arguments[field_name] = str(value)

    # 2. Handle JSON string inputs
    for field_name, value in arguments.items():
        if isinstance(value, str):
            try:
                parsed_value = json.loads(value)
                # logger.debug(f"Parsed {field_name} from JSON string: {parsed_value}")

                # specific array transformation
                if (isinstance(parsed_value, list) and
                    len(parsed_value) > 0 and
                    isinstance(parsed_value[0], dict) and
                    all(isinstance(v, (str, int, float, bool)) or v is None
                        for record in parsed_value
                        for v in record.values())):

                    transformed_records = []
                    for record in parsed_value:
                        transformed_record = {}
                        for k, v in record.items():
                            if isinstance(v, (int, float)) and not isinstance(v, bool):
                                v = str(v)
                            transformed_record[k] = {"value": v}
                        transformed_records.append(transformed_record)
                    parsed_value = transformed_records
                    # logger.debug(f"Transformed array records to API format: {parsed_value}")

                arguments[field_name] = parsed_value
            except json.JSONDecodeError as jde:
                # Try ast.literal_eval
                try:
                    import ast
                    parsed_value = ast.literal_eval(value)
                    if isinstance(parsed_value, (list, dict)):
                        arguments[field_name] = parsed_value
                        # logger.debug(f"Parsed {field_name} using ast.literal_eval")
                        continue
                except Exception:
                    pass
                logger.warning(f"Failed to parse {field_name} as JSON: {jde}, keeping as string")

    # 3. Force string conversion for numbers (final safety net)
    for arg_name, arg_value in list(arguments.items()):
        if isinstance(arg_value, (int, float)) and not isinstance(arg_value, bool):
            arguments[arg_name] = str(arg_value)
            # logger.debug(f"Force converting {arg_name} = {arg_value} ({type(arg_value).__name__}) to string")
########## MOD END ##########

def create_tool_coroutine(tool_name: str, arg_schema: type[BaseModel], client) -> Callable[..., Awaitable]:
    async def tool_coroutine(*args, **kwargs):
        # Get field names from the model (preserving order)
        field_names = list(arg_schema.model_fields.keys())
        provided_args = {}
        # Map positional arguments to their corresponding field names
        for i, arg in enumerate(args):
            if i >= len(field_names):
                msg = "Too many positional arguments provided"
                raise ValueError(msg)
            provided_args[field_names[i]] = arg
        # Merge in keyword arguments
        provided_args.update(kwargs)
        # Validate input and fill defaults for missing optional fields
        try:
########## MOD START ##########
            _fill_defaults(arg_schema, provided_args)
            # await logger.adebug(f"Tool '{tool_name}' input args: {provided_args}")
########## MOD END ##########

            validated = arg_schema.model_validate(provided_args)
        except Exception as e:
            msg = f"Invalid input: {e}"
            raise ValueError(msg) from e

        try:
########## MOD START ##########
            arguments = validated.model_dump()
            # await logger.adebug(f"Original arguments for {tool_name}: {arguments}")
            
            _post_process_arguments(arg_schema, arguments)
            
            # await logger.adebug(f"Final arguments for {tool_name}: {arguments}")
            return await client.run_tool(tool_name, arguments=arguments)
########## MOD END ##########
        except Exception as e:
            await logger.aerror(f"Tool '{tool_name}' execution failed: {e}")
            # Re-raise with more context
            msg = f"Tool '{tool_name}' execution failed: {e}"
            raise ValueError(msg) from e

    return tool_coroutine


def create_tool_func(tool_name: str, arg_schema: type[BaseModel], client) -> Callable[..., str]:
    def tool_func(*args, **kwargs):
        field_names = list(arg_schema.model_fields.keys())
        provided_args = {}
        for i, arg in enumerate(args):
            if i >= len(field_names):
                msg = "Too many positional arguments provided"
                raise ValueError(msg)
            provided_args[field_names[i]] = arg
        provided_args.update(kwargs)
        try:
########## MOD START ##########
            _fill_defaults(arg_schema, provided_args)
            # logger.debug(f"Tool '{tool_name}' input args: {provided_args}")
########## MOD END ##########
            validated = arg_schema.model_validate(provided_args)
        except Exception as e:
########## MOD START ##########
            logger.error(f"Tool validation error: {e}, provided: {provided_args}")
########## MOD END ##########
            msg = f"Invalid input: {e}"
            raise ValueError(msg) from e

        try:
########## MOD START ##########
            arguments = validated.model_dump()
            
            _post_process_arguments(arg_schema, arguments)
            
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(client.run_tool(tool_name, arguments=arguments))
########## MOD END ##########
        except Exception as e:
            logger.error(f"Tool '{tool_name}' execution failed: {e}")
            # Re-raise with more context
            msg = f"Tool '{tool_name}' execution failed: {e}"
            raise ValueError(msg) from e

    return tool_func


def get_unique_name(base_name, max_length, existing_names):
    name = base_name[:max_length]
    if name not in existing_names:
        return name
    i = 1
    while True:
        suffix = f"_{i}"
        truncated_base = base_name[: max_length - len(suffix)]
        candidate = f"{truncated_base}{suffix}"
        if candidate not in existing_names:
            return candidate
        i += 1


async def get_flow_snake_case(flow_name: str, user_id: str, session, *, is_action: bool | None = None) -> Flow | None:
    uuid_user_id = UUID(user_id) if isinstance(user_id, str) else user_id
    stmt = select(Flow).where(Flow.user_id == uuid_user_id).where(Flow.is_component == False)  # noqa: E712
    flows = (await session.exec(stmt)).all()

    for flow in flows:
        if is_action and flow.action_name:
            this_flow_name = sanitize_mcp_name(flow.action_name)
        else:
            this_flow_name = sanitize_mcp_name(flow.name)

        if this_flow_name == flow_name:
            return flow
    return None


def create_input_schema_from_json_schema(schema: dict[str, Any]) -> type[BaseModel]:
    """Dynamically build a Pydantic model from a JSON schema (with $defs).

    Non-required fields become Optional[...] with default=None.
    """
    if schema.get("type") != "object":
        msg = "Root schema must be type 'object'"
        raise ValueError(msg)

    defs: dict[str, dict[str, Any]] = schema.get("$defs", {})
    model_cache: dict[str, type[BaseModel]] = {}

    def resolve_ref(s: dict[str, Any] | None) -> dict[str, Any]:
        """Follow a $ref chain until you land on a real subschema."""
        if s is None:
            return {}
        while "$ref" in s:
            ref_name = s["$ref"].split("/")[-1]
            s = defs.get(ref_name)
            if s is None:
                logger.warning(f"Parsing input schema: Definition '{ref_name}' not found")
                return {"type": "string"}
        return s

########## MOD START ##########
    def is_complex_schema(schema: dict[str, Any]) -> bool:
        """Check if a schema is too complex for UI rendering."""
        # Check for additionalProperties with complex anyOf structures
        if "additionalProperties" in schema:
            additional_props = schema["additionalProperties"]
            if isinstance(additional_props, dict) and "anyOf" in additional_props:
                anyof_items = additional_props["anyOf"]
                # If anyOf has many items or contains complex objects
                if len(anyof_items) > 2:
                    return True
                for item in anyof_items:
                    if isinstance(item, dict) and item.get("type") == "object":
                        if "properties" in item and len(item["properties"]) > 3:
                            return True
                        if "additionalProperties" in item:
                            return True
            elif isinstance(additional_props, dict) and additional_props.get("type") == "object":
                return True

        # Check for complex object with many properties
        if schema.get("type") == "object" and "properties" in schema:
            properties_count = len(schema["properties"])
            if properties_count > 5:  # Threshold for complexity
                return True

        return False

    def get_fallback_type_for_complex_schema(schema: dict[str, Any]) -> Any:
        """Get an appropriate fallback type for complex schemas."""
        if schema.get("type") == "object":
            # For complex objects, use dict[str, Any] instead of str
            return dict[str, Any]
        elif schema.get("type") == "array":
            # For complex arrays, use list[dict[str, Any]]
            return list[dict[str, Any]]
        else:
            # Default fallback
            return str
########## MOD END ##########

    def parse_type(s: dict[str, Any] | None) -> Any:
        """Map a JSON Schema subschema to a Python type (possibly nested)."""
        if s is None:
            return None
        s = resolve_ref(s)
########## MOD START ##########
        # Handle boolean values in additionalProperties
        if "additionalProperties" in s and isinstance(s["additionalProperties"], bool):
            if s["additionalProperties"]:
                return dict[str, Any]
            # If false, it means no additional properties are allowed.
            # We can represent this by returning a type that won't be iterable.
            # However, for the purpose of building a model, we can perhaps return an empty dict
            # or handle it in the _build_model function.
            # For now, let's see if just handling `True` is enough.

        # Handle objects with additionalProperties (dynamic fields) but no explicit properties
        # This is common for dictionaries/maps where keys are dynamic
        if s.get("type") == "object" and "additionalProperties" in s and not s.get("properties"):
            # For dynamic dictionaries, returning Dict[str, Any] allows Langflow UI to potentially
            # render a JSON editor or Key-Value input.
            # Complex recursive types (like Unions) inside a Dict often cause UI rendering issues.
            return dict[str, Any]
########## MOD END ##########

        if "anyOf" in s:
            # Handle common pattern for nullable types (anyOf with string and null)
            subtypes = [sub.get("type") for sub in s["anyOf"] if isinstance(sub, dict) and "type" in sub]

            # Check if this is a simple nullable type (e.g., str | None)
            if len(subtypes) == NULLABLE_TYPE_LENGTH and "null" in subtypes:
                # Get the non-null type
                non_null_type = next(t for t in subtypes if t != "null")
                # Map it to Python type
                if isinstance(non_null_type, str):
                    return {
                        "string": str,
                        "integer": int,
                        "number": float,
                        "boolean": bool,
                        "object": dict,
                        "array": list,
                    }.get(non_null_type, Any)
                return Any

########## MOD START ##########
            ## For other anyOf cases, use the first non-null type
            # subtypes = [parse_type(sub) for sub in s["anyOf"]]
            # non_null_types = [t for t in subtypes if t is not None and t is not type(None)]
            # if non_null_types:
            #     return non_null_types[0]
            # return str

            # For other anyOf cases, return a Union of all possible types
            # This ensures Pydantic generates a schema with oneOf/anyOf, allowing the UI to render appropriate inputs
            try:
                subtypes = []
                for sub in s["anyOf"]:
                    parsed = parse_type(sub)
                    if parsed is not None and parsed is not type(None):
                        subtypes.append(parsed)
                
                # Remove duplicates while preserving order
                unique_types = []
                seen_types = set()
                for t in subtypes:
                    if t not in seen_types:
                        unique_types.append(t)
                        seen_types.add(t)

                if not unique_types:
                    return Any
                
                if len(unique_types) == 1:
                    return unique_types[0]
                
                # Safe Union creation for dynamic types
                # Using __getitem__ with a tuple is the standard way to create Union[A, B] dynamically
                # But we wrap in try-except to fallback to Any if type construction fails
                return Union[tuple(unique_types)]
            except Exception as e:
                logger.warning(f"Failed to create Union type from anyOf: {e}")
                return Any
########## MOD END ##########

        t = s.get("type", "any")  # Use string "any" as default instead of Any type
        if t == "array":
            item_schema = s.get("items", {})
########## MOD START ##########
            # schema_type: Any = parse_type(item_schema)
            # return list[schema_type]
            if item_schema:
                # Check for complex structures that UI cannot handle properly
                is_complex = False

                # Check if items schema has additionalProperties with anyOf (very complex)
                if "additionalProperties" in item_schema:
                    additional_props = item_schema["additionalProperties"]
                    if isinstance(additional_props, dict) and "anyOf" in additional_props:
                        anyof_items = additional_props.get("anyOf", [])
                        # If anyOf has more than 2 items or contains complex nested structures
                        if len(anyof_items) > 2:
                            is_complex = True
                        else:
                            # Check if anyOf items are complex objects themselves
                            for item in anyof_items:
                                if isinstance(item, dict) and item.get("type") == "object":
                                    is_complex = True
                                    break
                    elif item_schema.get("type") == "object" and "properties" in item_schema:
                        # Complex object with many properties
                        properties_count = len(item_schema.get("properties", {}))
                        if properties_count > 5:  # Threshold for complexity
                            is_complex = True

                # For complex array items, fall back to list[dict[str, Any]] instead of str
                # This ensures the field appears as an array input in the UI
                if is_complex:
                    logger.debug(f"Detected complex array schema, using list[dict[str, Any]] for UI compatibility")
                    return str  # Keep as str to force JSON input in UI

                schema_type: Any = parse_type(item_schema)
                return list[schema_type]

            return list[Any]
########## MOD END ##########

        if t == "object":
########## MOD START ##########
            # Check if object schema is too complex for UI rendering
            if is_complex_schema(s):
                logger.debug(f"Detected complex object schema, falling back to str (JSON input) for UI compatibility")
                return str
########## MOD END ##########
            # inline object not in $defs ⇒ anonymous nested model
            return _build_model(f"AnonModel{len(model_cache)}", s)

        # primitive fallback
        return {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
            "object": dict,
            "array": list,
        }.get(t, Any)

    def _build_model(name: str, subschema: dict[str, Any]) -> type[BaseModel]:
        """Create (or fetch) a BaseModel subclass for the given object schema."""
        # If this came via a named $ref, use that name
        if "$ref" in subschema:
            refname = subschema["$ref"].split("/")[-1]
            if refname in model_cache:
                return model_cache[refname]
            target = defs.get(refname)
            if not target:
                msg = f"Definition '{refname}' not found"
                raise ValueError(msg)
            cls = _build_model(refname, target)
            model_cache[refname] = cls
            return cls

        # Named anonymous or inline: avoid clashes by name
        if name in model_cache:
            return model_cache[name]

        props = subschema.get("properties", {})
        reqs = set(subschema.get("required", []))
        fields: dict[str, Any] = {}

        for prop_name, prop_schema in props.items():
            py_type = parse_type(prop_schema)
            is_required = prop_name in reqs
            if not is_required:
                py_type = py_type | None
                default = prop_schema.get("default", None)
            else:
                default = ...  # required by Pydantic

            fields[prop_name] = (py_type, Field(default, description=prop_schema.get("description")))

########## MOD START ##########
        # Handle additionalProperties for objects without explicit properties
        if "additionalProperties" in subschema:
            additional_props = subschema["additionalProperties"]
            if isinstance(additional_props, bool):
                if additional_props:
                    # Allow any additional properties - but this is complex for UI
                    # Fall back to str (JSON input) instead
                    logger.debug(f"Object '{name}' allows additional properties, using str (JSON input) for UI compatibility")
                    # Don't create fields, just return str type from parse_type
                    pass
            elif isinstance(additional_props, dict) and not props:
                # Handle dict-based additionalProperties
                additional_props_schema = resolve_ref(additional_props)
                if is_complex_schema(additional_props_schema) or "anyOf" in additional_props_schema:
                    # Complex additional properties - use str (JSON input)
                    logger.debug(f"Object '{name}' has complex additional properties, using str (JSON input) for UI compatibility")
                    pass  # Will be handled by falling back to str in caller
                else:
                    py_type = parse_type(additional_props_schema) or Any
                    fields["data"] = (Dict[str, py_type], Field(default_factory=dict, description="Dynamic field data"))
########## MOD END ##########

        model_cls = create_model(name, **fields)
        model_cache[name] = model_cls
        return model_cls

    # build the top - level "InputSchema" from the root properties
    top_props = schema.get("properties", {})
    top_reqs = set(schema.get("required", []))
    top_fields: dict[str, Any] = {}

    for fname, fdef in top_props.items():
        py_type = parse_type(fdef)
        if fname not in top_reqs:
            py_type = py_type | None
            default = fdef.get("default", None)
        else:
            default = ...
        top_fields[fname] = (py_type, Field(default, description=fdef.get("description")))
########## MOD START ##########
    # return create_model("InputSchema", **top_fields)

    final_model = create_model("InputSchema", **top_fields)
    # Patch deprecated schema method for Pydantic v2 compatibility
    final_model.schema = final_model.model_json_schema
    return final_model
########## MOD END ##########

def _is_valid_key_value_item(item: Any) -> bool:
    """Check if an item is a valid key-value dictionary."""
    return isinstance(item, dict) and "key" in item and "value" in item


def _process_headers(headers: Any) -> dict:
    """Process the headers input into a valid dictionary.

    Args:
        headers: The headers to process, can be dict, str, or list
    Returns:
        Processed and validated dictionary
    """
    if headers is None:
        return {}
    if isinstance(headers, dict):
        return validate_headers(headers)
    if isinstance(headers, list):
        processed_headers = {}
        try:
            for item in headers:
                if not _is_valid_key_value_item(item):
                    continue
                key = item["key"]
                value = item["value"]
                processed_headers[key] = value
        except (KeyError, TypeError, ValueError):
            return {}  # Return empty dictionary instead of None
        return validate_headers(processed_headers)
    return {}


def _validate_node_installation(command: str) -> str:
    """Validate the npx command."""
    if "npx" in command and not shutil.which("node"):
        msg = "Node.js is not installed. Please install Node.js to use npx commands."
        raise ValueError(msg)
    return command


async def _validate_connection_params(mode: str, command: str | None = None, url: str | None = None) -> None:
    """Validate connection parameters based on mode."""
    if mode not in ["Stdio", "SSE"]:
        msg = f"Invalid mode: {mode}. Must be either 'Stdio' or 'SSE'"
        raise ValueError(msg)

    if mode == "Stdio" and not command:
        msg = "Command is required for Stdio mode"
        raise ValueError(msg)
    if mode == "Stdio" and command:
        _validate_node_installation(command)
    if mode == "SSE" and not url:
        msg = "URL is required for SSE mode"
        raise ValueError(msg)


class MCPSessionManager:
    """Manages persistent MCP sessions with proper context manager lifecycle.

    Fixed version that addresses the memory leak issue by:
    1. Session reuse based on server identity rather than unique context IDs
    2. Maximum session limits per server to prevent resource exhaustion
    3. Idle timeout for automatic session cleanup
    4. Periodic cleanup of stale sessions
    """

    def __init__(self):
        # Structure: server_key -> {"sessions": {session_id: session_info}, "last_cleanup": timestamp}
        self.sessions_by_server = {}
        self._background_tasks = set()  # Keep references to background tasks
        # Backwards-compatibility maps: which context_id uses which (server_key, session_id)
        self._context_to_session: dict[str, tuple[str, str]] = {}
        # Reference count for each active (server_key, session_id)
        self._session_refcount: dict[tuple[str, str], int] = {}
        self._cleanup_task = None
        self._start_cleanup_task()

    def _start_cleanup_task(self):
        """Start the periodic cleanup task."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
            self._background_tasks.add(self._cleanup_task)
            self._cleanup_task.add_done_callback(self._background_tasks.discard)

    async def _periodic_cleanup(self):
        """Periodically clean up idle sessions."""
        while True:
            try:
                await asyncio.sleep(get_session_cleanup_interval())
                await self._cleanup_idle_sessions()
            except asyncio.CancelledError:
                break
            except (RuntimeError, KeyError, ClosedResourceError, ValueError, asyncio.TimeoutError) as e:
                # Handle common recoverable errors without stopping the cleanup loop
                await logger.awarning(f"Error in periodic cleanup: {e}")

    async def _cleanup_idle_sessions(self):
        """Clean up sessions that have been idle for too long."""
        current_time = asyncio.get_event_loop().time()
        servers_to_remove = []

        for server_key, server_data in self.sessions_by_server.items():
            sessions = server_data.get("sessions", {})
            sessions_to_remove = []

            for session_id, session_info in sessions.items():
                if current_time - session_info["last_used"] > get_session_idle_timeout():
                    sessions_to_remove.append(session_id)

            # Clean up idle sessions
            for session_id in sessions_to_remove:
                await logger.ainfo(f"Cleaning up idle session {session_id} for server {server_key}")
                await self._cleanup_session_by_id(server_key, session_id)

            # Remove server entry if no sessions left
            if not sessions:
                servers_to_remove.append(server_key)

        # Clean up empty server entries
        for server_key in servers_to_remove:
            del self.sessions_by_server[server_key]

    def _get_server_key(self, connection_params, transport_type: str) -> str:
        """Generate a consistent server key based on connection parameters."""
        if transport_type == "stdio":
########## MOD START ##########
            # if hasattr(connection_params, "command"):
            #     # Include command, args, and environment for uniqueness
            #     command_str = f"{connection_params.command} {' '.join(connection_params.args or [])}"
            #     env_str = str(sorted((connection_params.env or {}).items()))
            try:
                # Handle both object and dict formats for connection_params
                if isinstance(connection_params, dict):
                    # Dict format from UI
                    command = connection_params.get("command", "")
                    args = connection_params.get("args", [])
                    env = connection_params.get("env", {})
                    
                    # Safely convert list-type args to string
                    if isinstance(args, list):
                        args_str = " ".join(str(arg) for arg in args)
                        command_str = f"{command} {args_str}"
                    else:
                        command_str = f"{command} {args}"
                elif hasattr(connection_params, "command"):
                    # StdioServerParameters または同様のオブジェクト
                    command = getattr(connection_params, "command", "")
                    args_list = getattr(connection_params, "args", []) or []
                    env = getattr(connection_params, "env", {}) or {}
                    
                    # リスト型のargsを常に文字列に変換して安全に処理
                    if isinstance(args_list, list):
                        args_str = " ".join(str(arg) for arg in args_list)
                        command_str = f"{command} {args_str}"
                    else:
                        command_str = f"{command} {args_list}"
                else:
                    # Fallback
                    command_str = str(connection_params)
                    env = {}

                # Safely handle environment variables
                try:
                    if isinstance(env, dict):
                        # Convert dict keys and values to strings before sorting (safer)
                        env_items = sorted((str(k), str(v)) for k, v in env.items())
                        env_str = str(env_items)
                    else:
                        env_str = str(env)
                except (TypeError, AttributeError):
                    env_str = str(env)

########## MOD END ##########
                key_input = f"{command_str}|{env_str}"
                return f"stdio_{hash(key_input)}"
########## MOD START ##########
            except Exception:
                # Catch all exceptions and generate a fallback key
                # Generate a unique key using the object ID
                fallback_key = f"stdio_{hash(str(id(connection_params)))}"
                return fallback_key
########## MOD END ##########
        elif transport_type == "sse" and (isinstance(connection_params, dict) and "url" in connection_params):
            # Include URL and headers for uniqueness
            url = connection_params["url"]
########## MOD START ##########
            headers = connection_params.get("headers", {})

            # Handle case where headers might be a list instead of dict
            if isinstance(headers, list):
                # Convert list headers to dict if possible, otherwise convert to string
                try:
                    headers_dict = {}
                    for item in headers:
                        if isinstance(item, dict) and "key" in item and "value" in item:
                            headers_dict[item["key"]] = item["value"]
                        elif isinstance(item, str) and ":" in item:
                            # Parse "Key: Value" format
                            key, value = item.split(":", 1)
                            headers_dict[key.strip()] = value.strip()
                        else:
                            # Fallback: convert the entire list to string
                            headers_str = str(headers)
                            break
                    else:
                        headers_str = str(sorted(headers_dict.items()))
                except (ValueError, AttributeError):
                    headers_str = str(headers)
            elif isinstance(headers, dict):
                headers_str = str(sorted(headers.items()))
            else:
                headers_str = str(headers)

            # key_input = f"{url}|{headers}"
            key_input = f"{url}|{headers_str}"
            headers = connection_params.get("headers", {})

            # Handle case where headers might be a list instead of dict
            if isinstance(headers, list):
                # Convert list headers to dict if possible, otherwise convert to string
                try:
                    headers_dict = {}
                    for item in headers:
                        if isinstance(item, dict) and "key" in item and "value" in item:
                            headers_dict[item["key"]] = item["value"]
                        elif isinstance(item, str) and ":" in item:
                            # Parse "Key: Value" format
                            key, value = item.split(":", 1)
                            headers_dict[key.strip()] = value.strip()
                        else:
                            # Fallback: convert the entire list to string
                            headers_str = str(headers)
                            break
                    else:
                        headers_str = str(sorted(headers_dict.items()))
                except (ValueError, AttributeError):
                    headers_str = str(headers)
            elif isinstance(headers, dict):
                headers_str = str(sorted(headers.items()))
            else:
                headers_str = str(headers)

            key_input = f"{url}|{headers_str}"
########## MOD END ##########
            return f"sse_{hash(key_input)}"

        # Fallback to a generic key
        # TODO: add option for streamable HTTP in future.
        return f"{transport_type}_{hash(str(connection_params))}"

    async def _validate_session_connectivity(self, session) -> bool:
        """Validate that the session is actually usable by testing a simple operation."""
        try:
            # Try to list tools as a connectivity test (this is a lightweight operation)
            # Use a shorter timeout for the connectivity test to fail fast
            response = await asyncio.wait_for(session.list_tools(), timeout=3.0)
        except (asyncio.TimeoutError, ConnectionError, OSError, ValueError) as e:
            await logger.adebug(f"Session connectivity test failed (standard error): {e}")
            return False
        except Exception as e:
            # Handle MCP-specific errors that might not be in the standard list
            error_str = str(e)
            if (
                "ClosedResourceError" in str(type(e))
                or "Connection closed" in error_str
                or "Connection lost" in error_str
                or "Connection failed" in error_str
                or "Transport closed" in error_str
                or "Stream closed" in error_str
            ):
                await logger.adebug(f"Session connectivity test failed (MCP connection error): {e}")
                return False
            # Re-raise unexpected errors
            await logger.awarning(f"Unexpected error in connectivity test: {e}")
            raise
        else:
            # Validate that we got a meaningful response
            if response is None:
                await logger.adebug("Session connectivity test failed: received None response")
                return False
            try:
                # Check if we can access the tools list (even if empty)
                tools = getattr(response, "tools", None)
                if tools is None:
                    await logger.adebug("Session connectivity test failed: no tools attribute in response")
                    return False
            except (AttributeError, TypeError) as e:
                await logger.adebug(f"Session connectivity test failed while validating response: {e}")
                return False
            else:
                await logger.adebug(f"Session connectivity test passed: found {len(tools)} tools")
                return True

    async def get_session(self, context_id: str, connection_params, transport_type: str):
        """Get or create a session with improved reuse strategy.

        The key insight is that we should reuse sessions based on the server
        identity (command + args for stdio, URL for SSE) rather than the context_id.
        This prevents creating a new subprocess for each unique context.
        """
########## MOD START ##########
        try:
########## MOD END ##########
            server_key = self._get_server_key(connection_params, transport_type)
########## MOD START ##########
        except TypeError:
            # Convert parameters to a safe format and retry
            try:
                if transport_type == "stdio":
                    if isinstance(connection_params, dict) and "args" in connection_params:
                        args = connection_params["args"]
                        if isinstance(args, list):
                            # Convert list to tuple
                            connection_params = dict(connection_params)
                            connection_params["args"] = tuple(str(arg) for arg in args)
                    elif hasattr(connection_params, "args"):
                        # Handle objects like StdioServerParameters
                        args = getattr(connection_params, "args", [])
                        if isinstance(args, list):
                            # Convert to dict if object attributes cannot be updated
                            connection_params_dict = {
                                "command": getattr(connection_params, "command", ""),
                                "args": tuple(str(arg) for arg in args),
                                "env": getattr(connection_params, "env", {})
                            }
                            connection_params = connection_params_dict
                # Retry
                server_key = self._get_server_key(connection_params, transport_type)
            except Exception:
                # Final fallback: generate hash from unique string
                fallback_str = f"{transport_type}_{context_id}_{id(connection_params)}"
                server_key = f"{transport_type}_{hash(fallback_str)}"
########## MOD END ##########

        # Ensure server entry exists
        if server_key not in self.sessions_by_server:
            self.sessions_by_server[server_key] = {"sessions": {}, "last_cleanup": asyncio.get_event_loop().time()}

        server_data = self.sessions_by_server[server_key]
        sessions = server_data["sessions"]

        # Try to find a healthy existing session
        for session_id, session_info in sessions.items():
            session = session_info["session"]
            task = session_info["task"]

            # Check if session is still alive
            if not task.done():
                # Update last used time
                session_info["last_used"] = asyncio.get_event_loop().time()

                # Quick health check
                if await self._validate_session_connectivity(session):
                    await logger.adebug(f"Reusing existing session {session_id} for server {server_key}")
                    # record mapping & bump ref-count for backwards compatibility
                    self._context_to_session[context_id] = (server_key, session_id)
                    self._session_refcount[(server_key, session_id)] = (
                        self._session_refcount.get((server_key, session_id), 0) + 1
                    )
                    return session
                await logger.ainfo(f"Session {session_id} for server {server_key} failed health check, cleaning up")
                await self._cleanup_session_by_id(server_key, session_id)
            else:
                # Task is done, clean up
                await logger.ainfo(f"Session {session_id} for server {server_key} task is done, cleaning up")
                await self._cleanup_session_by_id(server_key, session_id)

        # Check if we've reached the maximum number of sessions for this server
        if len(sessions) >= get_max_sessions_per_server():
            # Remove the oldest session
            oldest_session_id = min(sessions.keys(), key=lambda x: sessions[x]["last_used"])
            await logger.ainfo(
                f"Maximum sessions reached for server {server_key}, removing oldest session {oldest_session_id}"
            )
            await self._cleanup_session_by_id(server_key, oldest_session_id)

        # Create new session
        session_id = f"{server_key}_{len(sessions)}"
        await logger.ainfo(f"Creating new session {session_id} for server {server_key}")

        if transport_type == "stdio":
            session, task = await self._create_stdio_session(session_id, connection_params)
        elif transport_type == "sse":
            session, task = await self._create_sse_session(session_id, connection_params)
        else:
            msg = f"Unknown transport type: {transport_type}"
            raise ValueError(msg)

        # Store session info
        sessions[session_id] = {
            "session": session,
            "task": task,
            "type": transport_type,
            "last_used": asyncio.get_event_loop().time(),
        }

        # register mapping & initial ref-count for the new session
        self._context_to_session[context_id] = (server_key, session_id)
        self._session_refcount[(server_key, session_id)] = 1

        return session

    async def _create_stdio_session(self, session_id: str, connection_params):
        """Create a new stdio session as a background task to avoid context issues."""
        import asyncio

        from mcp.client.stdio import stdio_client

        # Create a future to get the session
        session_future: asyncio.Future[ClientSession] = asyncio.Future()

        async def session_task():
            """Background task that keeps the session alive."""
            try:
                async with stdio_client(connection_params) as (read, write):
                    session = ClientSession(read, write)
                    async with session:
                        await session.initialize()
                        # Signal that session is ready
                        session_future.set_result(session)

                        # Keep the session alive until cancelled
                        import anyio

                        event = anyio.Event()
                        try:
                            await event.wait()
                        except asyncio.CancelledError:
                            await logger.ainfo(f"Session {session_id} is shutting down")
            except Exception as e:  # noqa: BLE001
                if not session_future.done():
                    session_future.set_exception(e)

        # Start the background task
        task = asyncio.create_task(session_task())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        # Wait for session to be ready
        try:
            session = await asyncio.wait_for(session_future, timeout=10.0)
        except asyncio.TimeoutError as timeout_err:
            # Clean up the failed task
            if not task.done():
                task.cancel()
                import contextlib

                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._background_tasks.discard(task)
            msg = f"Timeout waiting for STDIO session {session_id} to initialize"
            await logger.aerror(msg)
            raise ValueError(msg) from timeout_err

        return session, task

    async def _create_sse_session(self, session_id: str, connection_params):
        """Create a new SSE session as a background task to avoid context issues."""
        import asyncio

        from mcp.client.sse import sse_client

        # Create a future to get the session
        session_future: asyncio.Future[ClientSession] = asyncio.Future()

        async def session_task():
            """Background task that keeps the session alive."""
            try:
                async with sse_client(
                    connection_params["url"],
                    connection_params["headers"],
                    connection_params["timeout_seconds"],
                    connection_params["sse_read_timeout_seconds"],
                ) as (read, write):
                    session = ClientSession(read, write)
                    async with session:
                        await session.initialize()
                        # Signal that session is ready
                        session_future.set_result(session)

                        # Keep the session alive until cancelled
                        import anyio

                        event = anyio.Event()
                        try:
                            await event.wait()
                        except asyncio.CancelledError:
                            await logger.ainfo(f"Session {session_id} is shutting down")
            except Exception as e:  # noqa: BLE001
                if not session_future.done():
                    session_future.set_exception(e)

        # Start the background task
        task = asyncio.create_task(session_task())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        # Wait for session to be ready
        try:
            session = await asyncio.wait_for(session_future, timeout=10.0)
        except asyncio.TimeoutError as timeout_err:
            # Clean up the failed task
            if not task.done():
                task.cancel()
                import contextlib

                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._background_tasks.discard(task)
            msg = f"Timeout waiting for SSE session {session_id} to initialize"
            await logger.aerror(msg)
            raise ValueError(msg) from timeout_err

        return session, task

    async def _cleanup_session_by_id(self, server_key: str, session_id: str):
        """Clean up a specific session by server key and session ID."""
        if server_key not in self.sessions_by_server:
            return

        server_data = self.sessions_by_server[server_key]
        # Handle both old and new session structure
        if isinstance(server_data, dict) and "sessions" in server_data:
            sessions = server_data["sessions"]
        else:
            # Handle old structure where sessions were stored directly
            sessions = server_data

        if session_id not in sessions:
            return

        session_info = sessions[session_id]
        try:
            # First try to properly close the session if it exists
            if "session" in session_info:
                session = session_info["session"]

                # Try async close first (aclose method)
                if hasattr(session, "aclose"):
                    try:
                        await session.aclose()
                        await logger.adebug("Successfully closed session %s using aclose()", session_id)
                    except Exception as e:  # noqa: BLE001
                        await logger.adebug("Error closing session %s with aclose(): %s", session_id, e)

                # If no aclose, try regular close method
                elif hasattr(session, "close"):
                    try:
                        # Check if close() is awaitable using inspection
                        if inspect.iscoroutinefunction(session.close):
                            # It's an async method
                            await session.close()
                            await logger.adebug("Successfully closed session %s using async close()", session_id)
                        else:
                            # Try calling it and check if result is awaitable
                            close_result = session.close()
                            if inspect.isawaitable(close_result):
                                await close_result
                                await logger.adebug(
                                    "Successfully closed session %s using awaitable close()", session_id
                                )
                            else:
                                # It's a synchronous close
                                await logger.adebug("Successfully closed session %s using sync close()", session_id)
                    except Exception as e:  # noqa: BLE001
                        await logger.adebug("Error closing session %s with close(): %s", session_id, e)

            # Cancel the background task which will properly close the session
            if "task" in session_info:
                task = session_info["task"]
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        await logger.ainfo(f"Cancelled task for session {session_id}")
        except Exception as e:  # noqa: BLE001
            await logger.awarning(f"Error cleaning up session {session_id}: {e}")
        finally:
            # Remove from sessions dict
            del sessions[session_id]

    async def cleanup_all(self):
        """Clean up all sessions."""
        # Cancel periodic cleanup task
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task

        # Clean up all sessions
        for server_key in list(self.sessions_by_server.keys()):
            server_data = self.sessions_by_server[server_key]
            # Handle both old and new session structure
            if isinstance(server_data, dict) and "sessions" in server_data:
                sessions = server_data["sessions"]
            else:
                # Handle old structure where sessions were stored directly
                sessions = server_data

            for session_id in list(sessions.keys()):
                await self._cleanup_session_by_id(server_key, session_id)

        # Clear the sessions_by_server structure completely
        self.sessions_by_server.clear()

        # Clear compatibility maps
        self._context_to_session.clear()
        self._session_refcount.clear()

        # Clear all background tasks
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        # Give a bit more time for subprocess transports to clean up
        # This helps prevent the BaseSubprocessTransport.__del__ warnings
        await asyncio.sleep(0.5)

    async def _cleanup_session(self, context_id: str):
        """Backward-compat cleanup by context_id.

        Decrements the ref-count for the session used by *context_id* and only
        tears the session down when the last context that references it goes
        away.
        """
        mapping = self._context_to_session.get(context_id)
        if not mapping:
            await logger.adebug(f"No session mapping found for context_id {context_id}")
            return

        server_key, session_id = mapping
        ref_key = (server_key, session_id)
        remaining = self._session_refcount.get(ref_key, 1) - 1

        if remaining <= 0:
            await self._cleanup_session_by_id(server_key, session_id)
            self._session_refcount.pop(ref_key, None)
        else:
            self._session_refcount[ref_key] = remaining

        # Remove the mapping for this context
        self._context_to_session.pop(context_id, None)


class MCPStdioClient:
    def __init__(self, component_cache=None):
        self.session: ClientSession | None = None
        self._connection_params = None
        self._connected = False
        self._session_context: str | None = None
        self._component_cache = component_cache

    async def _connect_to_server(self, command_str: str, env: dict[str, str] | None = None) -> list[StructuredTool]:
        """Connect to MCP server using stdio transport (SDK style)."""
        from mcp import StdioServerParameters

        command = command_str.split(" ")
        env_data: dict[str, str] = {"DEBUG": "true", "PATH": os.environ["PATH"], **(env or {})}

        if platform.system() == "Windows":
            server_params = StdioServerParameters(
                command="cmd",
                args=[
                    "/c",
                    f"{command[0]} {' '.join(command[1:])} || echo Command failed with exit code %errorlevel% 1>&2",
                ],
                env=env_data,
            )
        else:
            server_params = StdioServerParameters(
                command="bash",
                args=["-c", f"exec {command_str} || echo 'Command failed with exit code $?' >&2"],
                env=env_data,
            )

        # Store connection parameters for later use in run_tool
        self._connection_params = server_params

########## MOD START ##########
        # Avoid StdioServerParameters hashing issues
        try:
            # Safely patch class method
            if not hasattr(StdioServerParameters, '_patched_hash'):
                original_hash = StdioServerParameters.__hash__

                def safe_hash(self):
                    try:
                        return original_hash(self)
                    except TypeError as e:
                        if "unhashable type" in str(e):
                            # Convert list elements to strings and tuple-ize
                            args = getattr(self, 'args', [])
                            env = getattr(self, 'env', {})

                            # Convert only if args is a list
                            if isinstance(args, list):
                                hashable_args = tuple(str(arg) for arg in args)
                            else:
                                hashable_args = str(args)

                            # Convert only if env is a dict
                            if isinstance(env, dict):
                                hashable_env = tuple(sorted((str(k), str(v)) for k, v in env.items()))
                            else:
                                hashable_env = str(env)

                            command = getattr(self, 'command', '')
                            return hash((command, hashable_args, hashable_env))
                        raise

                # Replace class method
                StdioServerParameters.__hash__ = safe_hash
                StdioServerParameters._patched_hash = True
                await logger.adebug("Successfully patched StdioServerParameters.__hash__ to handle unhashable lists")
        except Exception as patch_e:
            await logger.adebug(f"Failed to patch StdioServerParameters.__hash__: {patch_e}")

        try:
            hash_value = hash(server_params)
            await logger.adebug(f"StdioServerParameters is hashable after patch: {hash_value}")
        except Exception as hash_e:
            await logger.adebug(f"StdioServerParameters is still NOT hashable: {hash_e}")
            # Handle individually if patch fails
            try:
                # Calculate hash directly
                args = getattr(server_params, 'args', [])
                env = getattr(server_params, 'env', {})
                command = getattr(server_params, 'command', '')

                if isinstance(args, list):
                    hashable_args = tuple(str(arg) for arg in args)
                else:
                    hashable_args = str(args)

                if isinstance(env, dict):
                    hashable_env = tuple(sorted((str(k), str(v)) for k, v in env.items()))
                else:
                    hashable_env = str(env)

                fallback_hash = hash((command, hashable_args, hashable_env))
                await logger.adebug(f"Calculated fallback hash: {fallback_hash}")

                # Set hash directly on the object
                server_params._hash_value = fallback_hash
                server_params.__hash__ = lambda self: self._hash_value
                await logger.adebug("Applied fallback hash method to server_params")
            except Exception as fallback_e:
                await logger.adebug(f"Failed to apply fallback hash: {fallback_e}")
########## MOD END ##########

        # If no session context is set, create a default one
        if not self._session_context:
            # Generate a fallback context based on connection parameters
            import uuid

            param_hash = uuid.uuid4().hex[:8]
            self._session_context = f"default_{param_hash}"

########## MOD START ##########
        # Get or create a persistent session
        await logger.adebug(f"MCPStdioClient._connect_to_server - getting session with context: {self._session_context}")
        session = await self._get_or_create_session()
        response = await session.list_tools()
        self._connected = True
        await logger.adebug(f"MCPStdioClient._connect_to_server - connected successfully with {len(response.tools)} tools")
        return response.tools
########## MOD END ##########

    async def connect_to_server(self, command_str: str, env: dict[str, str] | None = None) -> list[StructuredTool]:
        """Connect to MCP server using stdio transport (SDK style)."""
        return await asyncio.wait_for(
            self._connect_to_server(command_str, env), timeout=get_settings_service().settings.mcp_server_timeout
        )

    def set_session_context(self, context_id: str):
        """Set the session context (e.g., flow_id + user_id + session_id)."""
        self._session_context = context_id

    def _get_session_manager(self) -> MCPSessionManager:
        """Get or create session manager from component cache."""
        if not self._component_cache:
            # Fallback to instance-level session manager if no cache
            if not hasattr(self, "_session_manager"):
                self._session_manager = MCPSessionManager()
            return self._session_manager

        from langflow.services.cache.utils import CacheMiss

        session_manager = self._component_cache.get("mcp_session_manager")
        if isinstance(session_manager, CacheMiss):
            session_manager = MCPSessionManager()
            self._component_cache.set("mcp_session_manager", session_manager)
        return session_manager

    async def _get_or_create_session(self) -> ClientSession:
        """Get or create a persistent session for the current context."""
        if not self._session_context or not self._connection_params:
            msg = "Session context and connection params must be set"
            raise ValueError(msg)

        # Use cached session manager to get/create persistent session
        session_manager = self._get_session_manager()
        return await session_manager.get_session(self._session_context, self._connection_params, "stdio")

    async def run_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Run a tool with the given arguments using context-specific session.

        Args:
            tool_name: Name of the tool to run
            arguments: Dictionary of arguments to pass to the tool

        Returns:
            The result of the tool execution

        Raises:
            ValueError: If session is not initialized or tool execution fails
        """
        if not self._connected or not self._connection_params:
            msg = "Session not initialized or disconnected. Call connect_to_server first."
            raise ValueError(msg)

        # If no session context is set, create a default one
        if not self._session_context:
            # Generate a fallback context based on connection parameters
            import uuid

            param_hash = uuid.uuid4().hex[:8]
            self._session_context = f"default_{param_hash}"

        max_retries = 2
        last_error_type = None

        for attempt in range(max_retries):
            try:
                await logger.adebug(f"Attempting to run tool '{tool_name}' (attempt {attempt + 1}/{max_retries})")
                # Get or create persistent session
                session = await self._get_or_create_session()

                result = await asyncio.wait_for(
                    session.call_tool(tool_name, arguments=arguments),
                    timeout=30.0,  # 30 second timeout
                )
            except Exception as e:
                current_error_type = type(e).__name__
                await logger.awarning(f"Tool '{tool_name}' failed on attempt {attempt + 1}: {current_error_type} - {e}")

                # Import specific MCP error types for detection
                try:
                    is_closed_resource_error = isinstance(e, ClosedResourceError)
                    is_mcp_connection_error = isinstance(e, McpError) and "Connection closed" in str(e)
                except ImportError:
                    is_closed_resource_error = "ClosedResourceError" in str(type(e))
                    is_mcp_connection_error = "Connection closed" in str(e)

                # Detect timeout errors
                is_timeout_error = isinstance(e, asyncio.TimeoutError | TimeoutError)

                # If we're getting the same error type repeatedly, don't retry
                if last_error_type == current_error_type and attempt > 0:
                    await logger.aerror(f"Repeated {current_error_type} error for tool '{tool_name}', not retrying")
                    break

                last_error_type = current_error_type

                # If it's a connection error (ClosedResourceError or MCP connection closed) and we have retries left
                if (is_closed_resource_error or is_mcp_connection_error) and attempt < max_retries - 1:
                    await logger.awarning(
                        f"MCP session connection issue for tool '{tool_name}', retrying with fresh session..."
                    )
                    # Clean up the dead session
                    if self._session_context:
                        session_manager = self._get_session_manager()
                        await session_manager._cleanup_session(self._session_context)
                    # Add a small delay before retry
                    await asyncio.sleep(0.5)
                    continue

                # If it's a timeout error and we have retries left, try once more
                if is_timeout_error and attempt < max_retries - 1:
                    await logger.awarning(f"Tool '{tool_name}' timed out, retrying...")
                    # Don't clean up session for timeouts, might just be a slow response
                    await asyncio.sleep(1.0)
                    continue

                # For other errors or no retries left, handle as before
                if (
                    isinstance(e, ConnectionError | TimeoutError | OSError | ValueError)
                    or is_closed_resource_error
                    or is_mcp_connection_error
                    or is_timeout_error
                ):
                    msg = f"Failed to run tool '{tool_name}' after {attempt + 1} attempts: {e}"
                    await logger.aerror(msg)
                    # Clean up failed session from cache
                    if self._session_context and self._component_cache:
                        cache_key = f"mcp_session_stdio_{self._session_context}"
                        self._component_cache.delete(cache_key)
                    self._connected = False
                    raise ValueError(msg) from e
                # Re-raise unexpected errors
                raise
            else:
                await logger.adebug(f"Tool '{tool_name}' completed successfully")
                return result

        # This should never be reached due to the exception handling above
        msg = f"Failed to run tool '{tool_name}': Maximum retries exceeded with repeated {last_error_type} errors"
        await logger.aerror(msg)
        raise ValueError(msg)

    async def disconnect(self):
        """Properly close the connection and clean up resources."""
        # For stdio transport, there is no remote session to terminate explicitly
        # The session cleanup happens when the background task is cancelled

        # Clean up local session using the session manager
        if self._session_context:
            session_manager = self._get_session_manager()
            await session_manager._cleanup_session(self._session_context)

        # Reset local state
        self.session = None
        self._connection_params = None
        self._connected = False
        self._session_context = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()


class MCPSseClient:
    def __init__(self, component_cache=None):
        self.session: ClientSession | None = None
        self._connection_params = None
        self._connected = False
        self._session_context: str | None = None
        self._component_cache = component_cache

    def _get_session_manager(self) -> MCPSessionManager:
        """Get or create session manager from component cache."""
        if not self._component_cache:
            # Fallback to instance-level session manager if no cache
            if not hasattr(self, "_session_manager"):
                self._session_manager = MCPSessionManager()
            return self._session_manager

        from langflow.services.cache.utils import CacheMiss

        session_manager = self._component_cache.get("mcp_session_manager")
        if isinstance(session_manager, CacheMiss):
            session_manager = MCPSessionManager()
            self._component_cache.set("mcp_session_manager", session_manager)
        return session_manager

    async def validate_url(self, url: str | None, headers: dict[str, str] | None = None) -> tuple[bool, str]:
        """Validate the SSE URL before attempting connection."""
        try:
            parsed = urlparse(url)
            if not parsed.scheme or not parsed.netloc:
                return False, "Invalid URL format. Must include scheme (http/https) and host."

            async with httpx.AsyncClient() as client:
                try:
                    # For SSE endpoints, try a GET request with short timeout
                    # Many SSE servers don't support HEAD requests and return 404
                    response = await client.get(
                        url, timeout=2.0, headers={"Accept": "text/event-stream", **(headers or {})}
                    )

                    # For SSE, we expect the server to either:
                    # 1. Start streaming (200)
                    # 2. Return 404 if HEAD/GET without proper SSE handshake is not supported
                    # 3. Return other status codes that we should handle gracefully

                    # Don't fail on 404 since many SSE endpoints return this for non-SSE requests
                    if response.status_code == HTTP_NOT_FOUND:
                        # This is likely an SSE endpoint that doesn't support regular GET
                        # Let the actual SSE connection attempt handle this
                        return True, ""

                    # Fail on client errors except 404, but allow server errors and redirects
                    if (
                        HTTP_BAD_REQUEST <= response.status_code < HTTP_INTERNAL_SERVER_ERROR
                        and response.status_code != HTTP_NOT_FOUND
                    ):
                        return False, f"Server returned client error status: {response.status_code}"

                except httpx.TimeoutException:
                    # Timeout on a short request might indicate the server is trying to stream
                    # This is actually expected behavior for SSE endpoints
                    return True, ""
                except httpx.NetworkError:
                    return False, "Network error. Could not reach the server."
                else:
                    return True, ""

        except (httpx.HTTPError, ValueError, OSError) as e:
            return False, f"URL validation error: {e!s}"

    async def pre_check_redirect(self, url: str | None, headers: dict[str, str] | None = None) -> str | None:
        """Check for redirects and return the final URL."""
        if url is None:
            return url
        try:
            async with httpx.AsyncClient(follow_redirects=False) as client:
                # Use GET with SSE headers instead of HEAD since many SSE servers don't support HEAD
                response = await client.get(
                    url, timeout=2.0, headers={"Accept": "text/event-stream", **(headers or {})}
                )
                if response.status_code == httpx.codes.TEMPORARY_REDIRECT:
                    return response.headers.get("Location", url)
                # Don't treat 404 as an error here - let the main connection handle it
        except (httpx.RequestError, httpx.HTTPError) as e:
            await logger.awarning(f"Error checking redirects: {e}")
        return url

    async def _connect_to_server(
        self,
        url: str | None,
        headers: dict[str, str] | None = None,
        timeout_seconds: int = 30,
        sse_read_timeout_seconds: int = 30,
    ) -> list[StructuredTool]:
        """Connect to MCP server using SSE transport (SDK style)."""
        # Validate and sanitize headers early
        validated_headers = _process_headers(headers)

        if url is None:
            msg = "URL is required for SSE mode"
            raise ValueError(msg)
        is_valid, error_msg = await self.validate_url(url, validated_headers)
        if not is_valid:
            msg = f"Invalid SSE URL ({url}): {error_msg}"
            raise ValueError(msg)

        url = await self.pre_check_redirect(url, validated_headers)

        # Store connection parameters for later use in run_tool
        self._connection_params = {
            "url": url,
            "headers": validated_headers,
            "timeout_seconds": timeout_seconds,
            "sse_read_timeout_seconds": sse_read_timeout_seconds,
        }

        # If no session context is set, create a default one
        if not self._session_context:
            # Generate a fallback context based on connection parameters
            import uuid

            param_hash = uuid.uuid4().hex[:8]
            self._session_context = f"default_sse_{param_hash}"

        # Get or create a persistent session
        session = await self._get_or_create_session()
        response = await session.list_tools()
        self._connected = True
        return response.tools

    async def connect_to_server(self, url: str, headers: dict[str, str] | None = None) -> list[StructuredTool]:
        """Connect to MCP server using SSE transport (SDK style)."""
        return await asyncio.wait_for(
            self._connect_to_server(url, headers), timeout=get_settings_service().settings.mcp_server_timeout
        )

    def set_session_context(self, context_id: str):
        """Set the session context (e.g., flow_id + user_id + session_id)."""
        self._session_context = context_id

    async def _get_or_create_session(self) -> ClientSession:
        """Get or create a persistent session for the current context."""
        if not self._session_context or not self._connection_params:
            msg = "Session context and params must be set"
            raise ValueError(msg)

        # Use cached session manager to get/create persistent session
        session_manager = self._get_session_manager()
        # Cache session so we can access server-assigned session_id later for DELETE
        self.session = await session_manager.get_session(self._session_context, self._connection_params, "sse")
        return self.session

    async def _terminate_remote_session(self) -> None:
        """Attempt to explicitly terminate the remote MCP session via HTTP DELETE (best-effort)."""
        # Only relevant for SSE transport
        if not self._connection_params or "url" not in self._connection_params:
            return

        url: str = self._connection_params["url"]

        # Retrieve session id from the underlying SDK if exposed
        session_id = None
        if getattr(self, "session", None) is not None:
            # Common attributes in MCP python SDK: `session_id` or `id`
            session_id = getattr(self.session, "session_id", None) or getattr(self.session, "id", None)

        headers: dict[str, str] = dict(self._connection_params.get("headers", {}))
        if session_id:
            headers["Mcp-Session-Id"] = str(session_id)

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.delete(url, headers=headers)
        except Exception as e:  # noqa: BLE001
            # DELETE is advisory—log and continue
            logger.debug(f"Unable to send session DELETE to '{url}': {e}")

    async def run_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Run a tool with the given arguments using context-specific session.

        Args:
            tool_name: Name of the tool to run
            arguments: Dictionary of arguments to pass to the tool

        Returns:
            The result of the tool execution

        Raises:
            ValueError: If session is not initialized or tool execution fails
        """
        if not self._connected or not self._connection_params:
            msg = "Session not initialized or disconnected. Call connect_to_server first."
            raise ValueError(msg)

        # If no session context is set, create a default one
        if not self._session_context:
            # Generate a fallback context based on connection parameters
            import uuid

            param_hash = uuid.uuid4().hex[:8]
            self._session_context = f"default_sse_{param_hash}"

        max_retries = 2
        last_error_type = None

        for attempt in range(max_retries):
            try:
                await logger.adebug(f"Attempting to run tool '{tool_name}' (attempt {attempt + 1}/{max_retries})")
                # Get or create persistent session
                session = await self._get_or_create_session()

                # Add timeout to prevent hanging
                import asyncio

                result = await asyncio.wait_for(
                    session.call_tool(tool_name, arguments=arguments),
                    timeout=30.0,  # 30 second timeout
                )
            except Exception as e:
                current_error_type = type(e).__name__
                await logger.awarning(f"Tool '{tool_name}' failed on attempt {attempt + 1}: {current_error_type} - {e}")

                # Import specific MCP error types for detection
                try:
                    from anyio import ClosedResourceError
                    from mcp.shared.exceptions import McpError

                    is_closed_resource_error = isinstance(e, ClosedResourceError)
                    is_mcp_connection_error = isinstance(e, McpError) and "Connection closed" in str(e)
                except ImportError:
                    is_closed_resource_error = "ClosedResourceError" in str(type(e))
                    is_mcp_connection_error = "Connection closed" in str(e)

                # Detect timeout errors
                is_timeout_error = isinstance(e, asyncio.TimeoutError | TimeoutError)

                # If we're getting the same error type repeatedly, don't retry
                if last_error_type == current_error_type and attempt > 0:
                    await logger.aerror(f"Repeated {current_error_type} error for tool '{tool_name}', not retrying")
                    break

                last_error_type = current_error_type

                # If it's a connection error (ClosedResourceError or MCP connection closed) and we have retries left
                if (is_closed_resource_error or is_mcp_connection_error) and attempt < max_retries - 1:
                    await logger.awarning(
                        f"MCP session connection issue for tool '{tool_name}', retrying with fresh session..."
                    )
                    # Clean up the dead session
                    if self._session_context:
                        session_manager = self._get_session_manager()
                        await session_manager._cleanup_session(self._session_context)
                    # Add a small delay before retry
                    await asyncio.sleep(0.5)
                    continue

                # If it's a timeout error and we have retries left, try once more
                if is_timeout_error and attempt < max_retries - 1:
                    await logger.awarning(f"Tool '{tool_name}' timed out, retrying...")
                    # Don't clean up session for timeouts, might just be a slow response
                    await asyncio.sleep(1.0)
                    continue

                # For other errors or no retries left, handle as before
                if (
                    isinstance(e, ConnectionError | TimeoutError | OSError | ValueError)
                    or is_closed_resource_error
                    or is_mcp_connection_error
                    or is_timeout_error
                ):
                    msg = f"Failed to run tool '{tool_name}' after {attempt + 1} attempts: {e}"
                    await logger.aerror(msg)
                    # Clean up failed session from cache
                    if self._session_context and self._component_cache:
                        cache_key = f"mcp_session_sse_{self._session_context}"
                        self._component_cache.delete(cache_key)
                    self._connected = False
                    raise ValueError(msg) from e
                # Re-raise unexpected errors
                raise
            else:
                await logger.adebug(f"Tool '{tool_name}' completed successfully")
                return result

        # This should never be reached due to the exception handling above
        msg = f"Failed to run tool '{tool_name}': Maximum retries exceeded with repeated {last_error_type} errors"
        await logger.aerror(msg)
        raise ValueError(msg)

    async def disconnect(self):
        """Properly close the connection and clean up resources."""
        # Attempt best-effort remote session termination first
        await self._terminate_remote_session()

        # Clean up local session using the session manager
        if self._session_context:
            session_manager = self._get_session_manager()
            await session_manager._cleanup_session(self._session_context)

        # Reset local state
        self.session = None
        self._connection_params = None
        self._connected = False
        self._session_context = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()


async def update_tools(
    server_name: str,
    server_config: dict,
    mcp_stdio_client: MCPStdioClient | None = None,
    mcp_sse_client: MCPSseClient | None = None,
) -> tuple[str, list[StructuredTool], dict[str, StructuredTool]]:
    """Fetch server config and update available tools."""
    if server_config is None:
        server_config = {}
    if not server_name:
        return "", [], {}
    if mcp_stdio_client is None:
        mcp_stdio_client = MCPStdioClient()
    if mcp_sse_client is None:
        mcp_sse_client = MCPSseClient()

    # Fetch server config from backend
    mode = "Stdio" if "command" in server_config else "SSE" if "url" in server_config else ""
    command = server_config.get("command", "")
    url = server_config.get("url", "")
    tools = []
    headers = _process_headers(server_config.get("headers", {}))

    try:
        await _validate_connection_params(mode, command, url)
    except ValueError as e:
        logger.error(f"Invalid MCP server configuration for '{server_name}': {e}")
        raise

    # Determine connection type and parameters
    client: MCPStdioClient | MCPSseClient | None = None
    if mode == "Stdio":
        # Stdio connection
        args = server_config.get("args", [])
        env = server_config.get("env", {})
########## MOD START ##########
        # full_command = " ".join([command, *args])
        # Change: Safely handle list-type args
        if isinstance(args, list):
            args_str = [str(arg) for arg in args]
            full_command = " ".join([command] + args_str)
        else:
            # Handle case where args is not a list
            full_command = f"{command} {args}"
########## MOD END ##########
        tools = await mcp_stdio_client.connect_to_server(full_command, env)
        client = mcp_stdio_client
    elif mode == "SSE":
        # SSE connection
        tools = await mcp_sse_client.connect_to_server(url, headers=headers)
        client = mcp_sse_client
    else:
        logger.error(f"Invalid MCP server mode for '{server_name}': {mode}")
        return "", [], {}

    if not tools or not client or not client._connected:
        logger.warning(f"No tools available from MCP server '{server_name}' or connection failed")
        return "", [], {}

    tool_list = []
    tool_cache: dict[str, StructuredTool] = {}

########## MOD START ##########
    try:
########## MOD END ##########
        for tool in tools:
            if not tool or not hasattr(tool, "name"):
                continue
            try:
                args_schema = create_input_schema_from_json_schema(tool.inputSchema)
                if not args_schema:
                    logger.warning(f"Could not create schema for tool '{tool.name}' from server '{server_name}'")
                    continue

                tool_obj = StructuredTool(
                    name=tool.name,
                    description=tool.description or "",
                    args_schema=args_schema,
                    func=create_tool_func(tool.name, args_schema, client),
                    coroutine=create_tool_coroutine(tool.name, args_schema, client),
                    tags=[tool.name],
                    metadata={"server_name": server_name},
                )
                tool_list.append(tool_obj)
                tool_cache[tool.name] = tool_obj
            except (ConnectionError, TimeoutError, OSError, ValueError) as e:
                logger.error(f"Failed to create tool '{tool.name}' from server '{server_name}': {e}")
                msg = f"Failed to create tool '{tool.name}' from server '{server_name}': {e}"
                raise ValueError(msg) from e

########## MOD START ##########
            except TypeError as e:
                # Special handling for unhashable type errors
                if "unhashable type: 'list'" in str(e):
                    logger.warning(f"Unhashable type error when creating tool '{tool.name}' from server '{server_name}': {e}")
                    # Skip and continue
                    continue
                else:
                    raise
    except Exception as e:
        logger.error(f"Error updating tool list for server '{server_name}': {e}")
        # Return partially successful tools in case of list-type errors
        if "unhashable type: 'list'" in str(e):
            logger.warning(f"Returning partial tool list due to unhashable type error for server '{server_name}'")
            if tool_list:  # If at least one tool was created
                logger.info(f"Successfully loaded {len(tool_list)} tools from MCP server '{server_name}' (partial)")
                return mode, tool_list, tool_cache
        raise
########## MOD END ##########

    logger.info(f"Successfully loaded {len(tool_list)} tools from MCP server '{server_name}'")
    return mode, tool_list, tool_cache
