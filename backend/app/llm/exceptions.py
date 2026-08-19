class LLMError(Exception):
    """Base exception for all LLM gateway errors."""
    pass

class OllamaUnavailableError(LLMError):
    """Raised when the Ollama service is unreachable."""
    pass

class LLMTimeoutError(LLMError):
    """Raised when an LLM request exceeds the timeout limit."""
    pass

class ModelNotFoundError(LLMError):
    """Raised when the requested model is not found on Ollama."""
    pass

class MalformedResponseError(LLMError):
    """Raised when the LLM response is empty or invalid text."""
    pass

class InvalidJSONError(LLMError):
    """Raised when JSON parsing or Pydantic validation fails after retries."""
    pass

class ConnectionError(LLMError):
    """Raised on network/connection failures."""
    pass

class InferenceFailureError(LLMError):
    """Raised when Ollama encounters an internal inference failure."""
    pass

class InvalidTaskTypeError(LLMError):
    """Raised when an unrecognized task type is passed."""
    pass