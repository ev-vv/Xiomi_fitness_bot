"""Step 2: DuckDB views over the raw Parquet lake and Polars aggregation into an LLM-ready report."""

from .processor import Processor
from .report import Report

__all__ = ["Processor", "Report"]
