import base64
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
import uvicorn
from fastapi import FastAPI, Header, HTTPException, status
from loguru import logger
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

load_dotenv()

MODEL_NAME_OR_PATH = os.getenv("CHAT_RAG_MODEL_OR_PATH", "BAAI/bge-small-zh-v1.5")
"""Embedding model name or local path."""

PORT = int(os.getenv("OPENAI_EMBED_PORT", 21039))
"""Server port."""

RESPONSE_MODEL_NAME = Path(MODEL_NAME_OR_PATH).name
"""Model name to return in responses."""

API_KEY = "sk-embedding"
"""API key for authentication. Set to empty string to disable authentication."""


class EmbeddingsRequest(BaseModel):
    model: str = Field(..., description="Model name, OpenAI-compatible field")
    input: str | list[str] = Field(..., description="Input text or list of input texts")
    encoding_format: Literal["float", "base64"] = "float"
    user: str | None = None


class EmbeddingData(BaseModel):
    object: Literal["embedding"] = "embedding"
    embedding: list[float] | str
    index: int


class Usage(BaseModel):
    prompt_tokens: int
    total_tokens: int


class EmbeddingsResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[EmbeddingData]
    model: str
    usage: Usage


class ModelItem(BaseModel):
    id: str
    object: Literal["model"] = "model"
    owned_by: str = "local"


class ModelsResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelItem]


class ErrorBody(BaseModel):
    message: str
    type: str = "invalid_request_error"
    param: str | None = None
    code: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


def _split_inputs(value: str | list[str]) -> list[str]:
    if isinstance(value, str):
        return [value]

    if not value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "message": "'input' must not be an empty list",
                    "type": "invalid_request_error",
                    "param": "input",
                    "code": None,
                }
            },
        )

    for idx, item in enumerate(value):
        if not isinstance(item, str):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": {
                        "message": f"'input[{idx}]' must be a string",
                        "type": "invalid_request_error",
                        "param": "input",
                        "code": None,
                    }
                },
            )

    return value


def _tokens_for_text(model: SentenceTransformer, text: str) -> int:
    tokenizer = getattr(model, "tokenizer", None)
    if tokenizer is None:
        return max(1, len(text) // 4)

    encoded = tokenizer(
        text,
        add_special_tokens=True,
        truncation=False,
        return_attention_mask=False,
        return_token_type_ids=False,
    )
    input_ids = encoded.get("input_ids", [])
    return max(1, len(input_ids))


def _as_base64(embedding: list[float]) -> str:
    import struct

    binary = struct.pack(f"<{len(embedding)}f", *embedding)
    return base64.b64encode(binary).decode("ascii")


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Loading embedding model '{}'", MODEL_NAME_OR_PATH)
    app.state.embedding_model = SentenceTransformer(MODEL_NAME_OR_PATH)
    dimension = app.state.embedding_model.get_embedding_dimension()
    logger.info("Embedding model loaded, dimension={}", dimension)
    yield


app = FastAPI(title="OpenAI-Compatible Embedding Server", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models", response_model=ModelsResponse)
def list_models() -> ModelsResponse:
    return ModelsResponse(data=[ModelItem(id=RESPONSE_MODEL_NAME)])


@app.post(
    "/v1/embeddings",
    response_model=EmbeddingsResponse,
    responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}},
)
async def create_embeddings(
    request: EmbeddingsRequest,
    authorization: str | None = Header(default=None),
) -> EmbeddingsResponse:
    if API_KEY:
        expected = f"Bearer {API_KEY}"
        if authorization != expected:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": {
                        "message": "Invalid API key",
                        "type": "authentication_error",
                        "param": None,
                        "code": "invalid_api_key",
                    }
                },
            )

    if request.model != RESPONSE_MODEL_NAME:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "message": f"Model '{request.model}' not found. Available model: '{RESPONSE_MODEL_NAME}'",
                    "type": "invalid_request_error",
                    "param": "model",
                    "code": "model_not_found",
                }
            },
        )

    inputs = _split_inputs(request.input)
    model: SentenceTransformer = app.state.embedding_model

    vectors = model.encode(
        inputs,
        normalize_embeddings=False,
        show_progress_bar=False,
    )

    data: list[EmbeddingData] = []
    for idx, vector in enumerate(vectors.tolist()):
        embedding = vector if request.encoding_format == "float" else _as_base64(vector)
        data.append(EmbeddingData(index=idx, embedding=embedding))

    prompt_tokens = sum(_tokens_for_text(model, text) for text in inputs)
    return EmbeddingsResponse(
        data=data,
        model=RESPONSE_MODEL_NAME,
        usage=Usage(prompt_tokens=prompt_tokens, total_tokens=prompt_tokens),
    )


if __name__ == "__main__":
    uvicorn.run(app, port=PORT)
