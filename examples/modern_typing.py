#!/usr/bin/env python3
"""Modern Python Type Annotations Example.

This file demonstrates best practices for type annotations in Python 3.9+ projects.
"""

from __future__ import annotations  # For forward references

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

# Type variables
T = TypeVar("T")
R = TypeVar("R")


# Example class with modern type annotations
class DataProcessor:
    """Example class showing modern type annotations."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        cache_dir: Path | str | None = None,
    ) -> None:
        """Initialize with modern type annotations.

        Args:
            config: Optional configuration dictionary
            cache_dir: Optional cache directory path

        """
        self.config = config or {}
        self.cache_dir = Path(cache_dir) if cache_dir else Path.home() / ".cache"
        self.handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}

    def register_handler(self, name: str, handler: Callable[[dict[str, Any]], Any]) -> None:
        """Register a data handler function.

        Args:
            name: Handler name
            handler: Handler function that processes data

        """
        self.handlers[name] = handler

    def process_items(self, items: Sequence[dict[str, Any]]) -> list[Any]:
        """Process a sequence of items.

        Args:
            items: Sequence of data items to process

        Returns:
            List of processed results

        """
        results: list[Any] = []

        for item in items:
            handler_name = item.get("type", "default")
            handler = self.handlers.get(handler_name)

            if handler:
                try:
                    result = handler(item)
                    results.append(result)
                except Exception as e:
                    results.append({"error": str(e), "item": item})
            else:
                results.append({"error": f"No handler for type {handler_name}", "item": item})

        return results

    def batch_process(
        self,
        data_sources: Iterable[str | Path | dict[str, Any]],
        output_format: str = "dict",
    ) -> dict[str, list[Any]] | list[Any]:
        """Process multiple data sources.

        Args:
            data_sources: Iterable of file paths or data dictionaries
            output_format: Output format ("dict" or "list")

        Returns:
            Processed data in requested format

        """
        all_results: dict[str, list[Any]] = {}

        for source in data_sources:
            # Handle different input types
            if isinstance(source, str | Path):
                source_path = Path(source)
                source_name = source_path.stem

                try:
                    with source_path.open() as f:
                        items = json.load(f)
                except Exception as e:
                    all_results[source_name] = [{"error": f"Failed to load {source}: {e}"}]
                    continue
            else:
                source_name = source.get("name", "unnamed")
                items = source.get("items", [])

            # Process the items
            results = self.process_items(items if isinstance(items, list) else [items])
            all_results[source_name] = results

        # Return in requested format
        if output_format == "list":
            return [item for sublist in all_results.values() for item in sublist]

        return all_results

    @staticmethod
    def filter_results(
        results: Mapping[str, list[Any]],
        predicate: Callable[[Any], bool],
    ) -> dict[str, list[Any]]:
        """Filter results using a predicate function.

        Args:
            results: Results mapping
            predicate: Filter function

        Returns:
            Filtered results

        """
        filtered: dict[str, list[Any]] = {}

        for key, items in results.items():
            filtered[key] = [item for item in items if predicate(item)]

        return filtered


# Example usage
def main() -> None:
    """Example usage of the DataProcessor class with modern type annotations."""
    processor = DataProcessor()

    # Register handler functions with modern type annotations
    processor.register_handler(
        "user",
        lambda data: {"id": data.get("id"), "name": data.get("name"), "processed_at": datetime.now().isoformat()},
    )

    processor.register_handler(
        "order",
        lambda data: {
            "order_id": data.get("id"),
            "total": data.get("amount", 0) * 1.1,  # Add tax
            "processed_at": datetime.now().isoformat(),
        },
    )

    # Example data
    data_sources = [
        {
            "name": "users",
            "items": [{"type": "user", "id": 1, "name": "Alice"}, {"type": "user", "id": 2, "name": "Bob"}],
        },
        {
            "name": "orders",
            "items": [{"type": "order", "id": 101, "amount": 100}, {"type": "order", "id": 102, "amount": 200}],
        },
    ]

    # Process data using modern type annotations
    results = processor.batch_process(data_sources)

    # Filter results
    filtered_results = processor.filter_results(
        results,
        lambda x: isinstance(x, dict) and x.get("id") == 1 if "id" in x else False,
    )

    print(f"All results: {results}")
    print(f"Filtered results: {filtered_results}")


if __name__ == "__main__":
    main()
