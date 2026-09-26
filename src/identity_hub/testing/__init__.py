"""Тестовый адаптер Identity Hub без PostgreSQL."""

from identity_hub.testing.memory import MemoryStore, MemoryTx

__all__ = ["MemoryStore", "MemoryTx"]
