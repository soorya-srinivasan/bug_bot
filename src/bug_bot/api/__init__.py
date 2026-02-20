from fastapi import APIRouter

from . import admin

router = APIRouter()

__all__ = ["router", "admin"]

