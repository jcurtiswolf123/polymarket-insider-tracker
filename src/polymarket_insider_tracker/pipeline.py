"""Main pipeline orchestrator for Polymarket Insider Tracker.

This module provides the Pipeline class that wires together all detection
components and manages the event flow from ingestion to alerting.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, TypeVar

import httpx
from redis.asyncio import Redis

from polymarket_insider_tracker import terminal_errors as terminal
from polymarket_insider_tracker.alerter.dispatcher import DispatchResult
from polymarket_insider_tracker.assessment_row import assessment_dto, insert_despite_cancellation
from polymarket_insider_tracker.config import Settings, get_settings
from polymarket_insider_tracker.detector.detector_config import default_detector_config
from polymarket_insider_tracker.detector.evaluation import (
    DetectorError,
    SizeEvaluation,
    TradeEvidence,
    WalletEvaluation,
)
from polymarket_insider_tracker.detector.evaluation_clock import (
    Clock,
    read_evaluation_instant,
    system_clock,
)
from polymarket_insider_tracker.detector.insider_rules import (
    FreshCluster,
    RuleThresholds,
    TradeFeatures,
    dropped_for_activity,
    evaluate_tiered,
    is_active_trader,
    is_watch_market,
    widened,
)
from polymarket_insider_tracker.detector.learned import LearnedModel, append_features
from polymarket_insider_tracker.detector.models import RiskAssessment
from polymarket_insider_tracker.detector.scorer import SignalBundle
from polymarket_insider_tracker.ingestor.alert_path_health import AlertPathHealthMonitor
from polymarket_insider_tracker.ingestor.health import ComponentStatus, HealthMonitor
from polymarket_insider_tracker.ingestor.readiness import running_ingestion_status
from polymarket_insider_tracker.ingestor.trade_poller import IngestionState, TradePoller
from polymarket_insider_tracker.pipeline_components import (
    PipelineComponents,
    build_components,
    ping_redis,
)
from polymarket_insider_tracker.profiler.funding import FundingTracer
from polymarket_insider_tracker.profiler.wallet_history import (
    WalletHistory,
    features_for,
    fetch_wallet_history,
)
from polymarket_insider_tracker.redaction import redact_exception_message, redact_text
from polymarket_insider_tracker.storage.database import DatabaseManager
from polymarket_insider_tracker.storage.repos import (
    FundingRepository,
    FundingTransferDTO,
    RiskAssessmentRepository,
    WalletProfileDTO,
    WalletRepository,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from polymarket_insider_tracker.alerter.models import FormattedAlert
    from polymarket_insider_tracker.detector.models import FreshWalletSignal
    from polymarket_insider_tracker.ingestor.models import TradeEvent

logger = logging.getLogger(__name__)

_EvaluationT = TypeVar("_EvaluationT", covariant=True)


async def _run_detector(
    detector: str, trade: TradeEvent, evaluation: Awaitable[_EvaluationT]
) -> tuple[_EvaluationT | None, DetectorError | None]:
    """Await one detector's evaluation; a failure is logged redacted and returned, not raised."""
    try:
        return await evaluation, None
    except Exception as e:
        message = redact_text(
            f"{detector.replace('_', ' ')} detection failed for trade {trade.trade_id}:"
            f" {redact_exception_message(e)}"
        )
        logger.warning("%s", message)
        return None, DetectorError(detector, type(e).__name__, message)


def _signal_bundle(trade: TradeEvent, evidence: TradeEvidence) -> SignalBundle:
    """The bundle the scorer reads: each evaluation's signal, with the evidence behind them."""
    wallet, size = evidence.wallet, evidence.size
    fresh_signal = wallet.signal if wallet is not None else None
    return SignalBundle(trade, fresh_signal, size.signal if size is not None else None, evidence)


def _failures_only(evidence: TradeEvidence) -> TradeEvidence:
    """A skip row records its detector failures and no evaluation evidence."""
    return TradeEvidence(
        evidence.evaluated_at, evidence.detector_config, failures=evidence.failures
    )


class PipelineState(StrEnum):
    """Pipeline lifecycle states."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass
class _InstalledComponents:
    """The installed set the trade path and metadata crawl read. A re-install replaces ``current``
    in place, so a worker holding it reads whichever set is installed at the moment it reads.
    """

    current: PipelineComponents


@dataclass
class PipelineStats:
    """Statistics for the pipeline."""

    started_at: datetime | None = None
    trades_processed: int = 0
    signals_generated: int = 0
    alerts_sent: int = 0
    errors: int = 0
    last_trade_time: datetime | None = None
    last_error: str | None = None


class Pipeline:
    """Main pipeline orchestrator for the Polymarket Insider Tracker.

    This class wires together all detection components and manages the
    event flow from trade ingestion through profiling, detection, and alerting.

    Pipeline flow:
        Public trades query (TradePoller) → Wallet Profiler → Detectors → Risk Scorer → Alerter

    The poller starts before the market-metadata crawl completes; metadata is enrichment and
    never holds ingestion back.

    Example:
        ```python
        from polymarket_insider_tracker.config import get_settings
        from polymarket_insider_tracker.pipeline import Pipeline

        settings = get_settings()
        pipeline = Pipeline(settings)

        await pipeline.start()
        # Pipeline runs until stop() is called
        await pipeline.stop()
        ```
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        dry_run: bool | None = None,
    ) -> None:
        """Initialize the pipeline.

        Args:
            settings: Application settings. If not provided, uses get_settings().
            dry_run: If True, skip sending alerts. Overrides settings.dry_run.
        """
        self._settings = settings or get_settings()
        self._dry_run = dry_run if dry_run is not None else self._settings.dry_run

        self._state = PipelineState.STOPPED
        self._stats = PipelineStats()

        # Components (initialized in start())
        self._redis: Redis | None = None
        self._db_manager: DatabaseManager | None = None
        self._owns_connections = True
        self._components_installed = False
        self._installed: _InstalledComponents | None = None
        self._detector_config = default_detector_config()
        self._clock: Clock = system_clock
        self._trade_poller: TradePoller | None = None
        self._funding_tracer: FundingTracer | None = None
        self._health_monitor = AlertPathHealthMonitor(self._settings.detector.alert_threshold)
        self._wire_health_monitor()

        # Synchronization
        self._stop_event: asyncio.Event | None = None
        self._poller_task: asyncio.Task[None] | None = None
        self._metadata_task: asyncio.Task[None] | None = None
        self._terminal_writes: set[asyncio.Task[None]] = set()
        # Local fork: TRADE_WORKERS>0 scores trades on a worker pool so a slow market or
        # RPC lookup never stalls acquisition. 0 keeps upstream's inline processing.
        self._history_cache: dict[str, tuple[float, WalletHistory]] = {}
        self._fresh_cluster = FreshCluster()
        # Local fork: market start/end times from Gamma (in-play check), and the learned scorer.
        self._market_meta: dict[str, tuple[float, dict[str, object]]] = {}
        self._learned = LearnedModel()
        self._history_client: httpx.AsyncClient | None = None
        self._trade_workers = max(0, int(os.environ.get("TRADE_WORKERS", "0") or 0))
        self._trade_queue: asyncio.Queue[TradeEvent] | None = None
        # Trades below this USDC notional skip scoring entirely (0 = score everything). An alert
        # needs the fresh-wallet signal, whose own floor is $1,000, so a floor up to that loses
        # no alert; it keeps the worker pool ahead of Polymarket's full trade rate.
        self._min_score_notional = Decimal(os.environ.get("MIN_SCORE_NOTIONAL", "0") or "0")
        self._worker_tasks: list[asyncio.Task[None]] = []

    @property
    def settings(self) -> Settings:
        """The application settings this pipeline runs with."""
        return self._settings

    @property
    def dry_run(self) -> bool:
        """Whether alerts are only logged: the constructor override, else the settings flag."""
        return self._dry_run

    @property
    def state(self) -> PipelineState:
        """Current pipeline state."""
        return self._state

    @property
    def stats(self) -> PipelineStats:
        """Current pipeline statistics."""
        return self._stats

    @property
    def is_running(self) -> bool:
        """Check if pipeline is running."""
        return self._state == PipelineState.RUNNING

    @property
    def health_monitor(self) -> HealthMonitor:
        """Return the pipeline's health monitor."""
        return self._health_monitor

    @property
    def stop_event(self) -> asyncio.Event | None:
        """Event signaled when the pipeline stops or encounters an unrecoverable worker failure."""
        return self._stop_event

    @property
    def db_manager(self) -> DatabaseManager | None:
        """Database manager used for durable terminal rows."""
        return self._db_manager

    @property
    def terminal_writes(self) -> set[asyncio.Task[None]]:
        """Retained durable writes drained across stop/shutdown."""
        return self._terminal_writes

    def record_processing_error(self, message: str) -> None:
        """Count a recoverable per-trade failure and surface it at /health (lifecycle §5)."""
        self._record_processing_error(message)

    def _wire_health_monitor(self) -> None:
        self._health_monitor.set_component_checker("database", self._check_database)
        self._health_monitor.set_component_checker("redis", self._check_redis)
        self._health_monitor.set_component_checker("ingestion", self._check_ingestion)
        self._health_monitor.set_last_error_provider(lambda: self._stats.last_error)

    async def _check_database(self) -> ComponentStatus:
        if not self._db_manager:
            return ComponentStatus(status="down", last_error="Database manager not initialized")
        start = time.perf_counter()
        try:
            import sqlalchemy as sa

            async with self._db_manager.get_async_session() as session:
                await session.execute(sa.text("SELECT 1"))
            latency = (time.perf_counter() - start) * 1000.0
            return ComponentStatus(status="up", latency_ms=round(latency, 2))
        except Exception as exc:
            return ComponentStatus(status="down", last_error=redact_exception_message(exc))

    async def _check_redis(self) -> ComponentStatus:
        if not self._redis:
            return ComponentStatus(status="down", last_error="Redis client not initialized")
        start = time.perf_counter()
        try:
            await ping_redis(self._redis)
            latency = (time.perf_counter() - start) * 1000.0
            return ComponentStatus(status="up", latency_ms=round(latency, 2))
        except Exception as exc:
            return ComponentStatus(status="down", last_error=redact_exception_message(exc))

    def _sync_poller_timestamps(self) -> None:
        if not self._trade_poller:
            return
        status = self._trade_poller.status
        # Only a completed fetch proves the source was reached; the request-start
        # timestamp (last_acquisition_at) must never masquerade as acquisition
        # freshness when the request itself failed.
        if status.last_success_at is not None:
            self._health_monitor.record_acquisition(status.last_success_at.timestamp())
        if status.last_trade_at is not None:
            self._health_monitor.record_trade_arrival(status.last_trade_at.timestamp())

    async def _check_ingestion(self) -> ComponentStatus:
        self._sync_poller_timestamps()
        if not self._trade_poller:
            return ComponentStatus(status="down", last_error="Trade poller not initialized")
        if self._state == PipelineState.ERROR or self._trade_poller.state is IngestionState.FAILED:
            reason = await terminal.resolve_terminal_reason_live(
                self.db_manager,
                self._trade_poller.status.last_error,
                self._state == PipelineState.ERROR,
                self._stats.last_error,
            )
            return ComponentStatus(status="down", last_error=reason)
        if not self._trade_poller.is_running:
            return ComponentStatus(status="down", last_error="Trade poller stopped")
        return running_ingestion_status(self._trade_poller, self._health_monitor)

    async def check_readiness(self) -> tuple[bool, str | None, dict[str, str]]:
        """Evaluate current readiness across all dependencies."""
        components = await self._health_monitor.evaluate_components()
        all_up = HealthMonitor.all_components_up(components)
        summary = HealthMonitor.components_summary(components)
        reason = None
        if not all_up:
            reason = HealthMonitor.first_failure_reason(components)
        return all_up, reason, summary

    async def start(self) -> None:
        """Start the pipeline.

        Initializes all components and begins processing trades.

        Raises:
            RuntimeError: If pipeline is already running.
            Exception: If any component fails to initialize.
        """
        if self._state != PipelineState.STOPPED:
            raise RuntimeError(f"Cannot start pipeline in state {self._state}")

        self._state = PipelineState.STARTING
        self._stop_event = asyncio.Event()
        logger.info("Starting pipeline...")

        try:
            await self._installed_or_built()
            await self._start_background_services()
            self._stats.started_at = datetime.now(UTC)
            # A worker may already have failed terminally during startup; never
            # overwrite that ERROR state with RUNNING.
            if self._state == PipelineState.STARTING:
                self._state = PipelineState.RUNNING
                logger.info("Pipeline started successfully")
        except Exception as e:
            self._state = PipelineState.ERROR
            self._stats.last_error = redact_exception_message(e)
            logger.error("Failed to start pipeline: %s", self._stats.last_error)
            await self._cleanup()
            raise

    async def stop(self) -> None:
        """Stop the pipeline gracefully.

        Stops all background services and cleans up resources.
        """
        if self._state == PipelineState.STOPPED:
            return

        was_error = self._state == PipelineState.ERROR
        self._state = PipelineState.STOPPING
        logger.info("Stopping pipeline...")

        if self._stop_event:
            self._stop_event.set()

        await self._stop_background_services()
        await self._cleanup()

        self._state = PipelineState.ERROR if was_error else PipelineState.STOPPED
        logger.info("Pipeline stopped")

    async def _installed_or_built(self) -> _InstalledComponents:
        """The installed set, building and installing the production one when none is."""
        if self._installed is not None and self._components_installed:
            return self._installed
        return await self._initialize_components()

    async def _initialize_components(self) -> _InstalledComponents:
        """Build the production components from the settings and install them."""
        components = await build_components(
            self._settings,
            dry_run=self._dry_run,
            on_trade=self._on_polled_trade,
            on_ingestion_state=self.on_ingestion_state,
        )
        installed = self._install(components)
        logger.info("All components initialized")
        return installed

    def install_components(self, components: PipelineComponents) -> None:
        """Run on ``components`` instead of building them from the settings.

        ``start`` builds and installs the production set unless one is already installed, so an
        embedding caller can supply its own connections and collaborators. Connections the
        pipeline did not create (``owns_connections=False``) stay open when it stops.
        """
        self._install(components)

    def _install(self, components: PipelineComponents) -> _InstalledComponents:
        self._redis = components.redis
        self._db_manager = components.db_manager
        self._owns_connections = components.owns_connections
        if self._installed is None:
            self._installed = _InstalledComponents(components)
        self._installed.current = components
        self._funding_tracer = components.funding_tracer
        self._detector_config = components.detector_config
        self._trade_poller = components.trade_poller
        self._health_monitor.bind_scorer(components.risk_scorer, logger)
        self._components_installed = True
        return self._installed

    async def _start_health_server(self) -> None:
        self._wire_health_monitor()
        await self._health_monitor.start()
        try:
            await self._health_monitor.start_http_server(port=self._settings.health_port)
        except OSError as exc:
            logger.error(
                "Failed to bind health server on port %d: %s",
                self._settings.health_port,
                exc,
            )
            raise

    async def _start_background_services(self) -> None:
        """Start the poller first, then the metadata crawl on the set ``start`` installed."""
        installed = await self._installed_or_built()
        await self._start_health_server()

        if self._trade_poller:
            logger.debug("Starting trade poller...")
            self._poller_task = asyncio.create_task(self._run_trade_poller())

        logger.debug("Starting metadata sync service in the background...")
        self._metadata_task = asyncio.create_task(self._run_metadata_sync(installed))

        if self._trade_workers:
            self._trade_queue = asyncio.Queue(maxsize=self._trade_workers * 500)
            self._worker_tasks = [
                asyncio.create_task(self._trade_worker()) for _ in range(self._trade_workers)
            ]
            logger.info("trade worker pool: %d workers", self._trade_workers)
            self._worker_tasks.append(asyncio.create_task(self._report_queue()))

    async def _report_queue(self) -> None:
        while True:
            await asyncio.sleep(30)
            queue = self._trade_queue
            if queue is not None:
                logger.info(
                    "trade queue depth=%d processed=%d signals=%d",
                    queue.qsize(),
                    self._stats.trades_processed,
                    self._stats.signals_generated,
                )

    async def _on_polled_trade(self, trade: TradeEvent) -> None:
        """Poller callback: hand off to the worker pool when enabled, else score inline."""
        if self._min_score_notional and trade.price * trade.size < self._min_score_notional:
            self._stats.trades_processed += 1
            return
        if self._trade_queue is None:
            await self.process_trade(trade)
            return
        await self._trade_queue.put(trade)

    async def _trade_worker(self) -> None:
        queue = self._trade_queue
        assert queue is not None
        while True:
            trade = await queue.get()
            try:
                await self.process_trade(trade)
            finally:
                queue.task_done()

    def _record_processing_error(self, message: str) -> None:
        """Count a recoverable per-trade failure and surface it at /health (lifecycle §5)."""
        redacted = redact_text(message)
        logger.warning(redacted)
        self._stats.errors += 1
        self._stats.last_error = redacted

    def _handle_worker_failure(
        self, error: str, worker: str = terminal.DEFAULT_TERMINAL_WORKER
    ) -> None:
        """Handle background worker crash or terminal failure."""
        terminal.record_terminal_failure(self, error, worker)

    def enter_terminal_state(self) -> None:
        """Move to ERROR; bookkeeping lives in terminal_errors."""
        self._state = PipelineState.ERROR

    async def _drain_terminal_writes(self) -> None:
        """Await retained failure writes across stop/shutdown. Never raises."""
        await terminal.drain_terminal_writes(self.terminal_writes, self.record_processing_error)

    async def _run_trade_poller(self) -> None:
        """Run the poller in a task; a terminal failure is reported through the state callback."""
        if not self._trade_poller:
            return
        try:
            await self._trade_poller.start()
        except asyncio.CancelledError:
            logger.debug("Trade poller task cancelled")
        except Exception as e:
            logger.error("Trade poller error: %s", redact_exception_message(e))
            self._handle_worker_failure(redact_exception_message(e))

    async def _run_metadata_sync(self, installed: _InstalledComponents) -> None:
        """Run the initial metadata crawl without delaying acquisition."""
        delay = 30.0
        while True:
            try:
                await installed.current.metadata_sync.start()
                return
            except asyncio.CancelledError:
                logger.debug("Metadata sync task cancelled")
                return
            except Exception as e:
                # A failed first crawl used to end metadata sync for the process lifetime,
                # leaving outcome/niche blank until restart. Retry with backoff instead.
                logger.error(
                    "Metadata sync error (retry in %.0fs): %s", delay, redact_exception_message(e)
                )
                self._stats.last_error = redact_exception_message(e)
                self._stats.errors += 1
                await asyncio.sleep(delay)
                delay = min(delay * 2, 600.0)

    def on_ingestion_state(self, state: IngestionState) -> None:
        """Record terminal ingestion failures on the pipeline statistics."""
        if state is not IngestionState.FAILED or not self._trade_poller:
            return
        terminal.note_poller_failure(self, self._trade_poller.status.last_error)

    async def _stop_background_services(self) -> None:
        """Stop the poller and its task before the metadata task and sync."""
        if self._trade_poller:
            logger.debug("Stopping trade poller...")
            await self._trade_poller.stop()
        await terminal.cancel_task(self._poller_task)
        await terminal.cancel_task(self._metadata_task)
        for task in self._worker_tasks:
            await terminal.cancel_task(task)
        self._worker_tasks = []
        self._trade_queue = None
        if self._history_client is not None:
            await self._history_client.aclose()
            self._history_client = None
        self._poller_task = None
        self._metadata_task = None
        await self._drain_terminal_writes()
        if self._installed is not None:
            logger.debug("Stopping metadata sync...")
            await self._installed.current.metadata_sync.stop()
        await self._health_monitor.stop()

    async def _cleanup(self) -> None:
        """Clean up resources."""
        await self._drain_terminal_writes()
        await self._health_monitor.stop()

        if self._owns_connections:
            await self._close_connections()

        logger.debug("Resources cleaned up")

    async def _close_connections(self) -> None:
        """Close the connections the pipeline created; the next start builds them afresh."""
        if self._db_manager:
            await self._db_manager.dispose_async()
            self._db_manager = None

        if self._redis:
            await self._redis.aclose()
            self._redis = None

        self._components_installed = False

    async def process_trade(self, trade: TradeEvent) -> None:
        """Process a single trade event.

        This is the main event handler that runs the detection pipeline:
        1. Run fresh wallet detection
        2. Run size anomaly detection
        3. Score the combined signals
        4. Send alert if threshold exceeded

        Args:
            trade: The trade observation delivered by the poller.
        """
        self._stats.trades_processed += 1
        self._stats.last_trade_time = datetime.now(UTC)
        self._health_monitor.record_event("trades")

        try:
            await self._detect_score_and_alert(trade)
        except Exception as e:
            self._record_processing_error(
                f"Error processing trade {trade.trade_id}: {redact_exception_message(e)}"
            )

    async def _detect_score_and_alert(self, trade: TradeEvent) -> None:
        """Run detectors, count their failures, then score/alert or persist a skip row."""
        installed = self._installed
        if installed is None:
            # Before the first install there is no detector, so no signal and no failure.
            return
        evidence = await self._evaluate(trade, installed)
        bundle = _signal_bundle(trade, evidence)
        fresh_signal = bundle.fresh_wallet_signal

        # Persist wallet profile and funding data when a fresh wallet is detected
        if fresh_signal is not None:
            await self._persist_wallet_and_funding(fresh_signal)

        rules, suppress = await self._rules_and_gate(trade, bundle)
        if fresh_signal or bundle.size_anomaly_signal or rules:
            self._stats.signals_generated += 1
            await self._score_and_alert(bundle, installed, rules, suppress=suppress)
        elif evidence.failures:
            await self._persist_detector_failure_skip(bundle, evidence, installed)

    async def _evaluate(self, trade: TradeEvent, installed: _InstalledComponents) -> TradeEvidence:
        """Run both detectors in parallel at one evaluation instant; count their failures."""
        # One evaluation instant per trade, shared by both detectors (detector-api.md section 2)
        evaluated_at = read_evaluation_instant(self._clock)
        (wallet, fresh_error), (size, size_error) = await asyncio.gather(
            self._detect_fresh_wallet(trade, evaluated_at, installed),
            self._detect_size_anomaly(trade, evaluated_at, installed),
        )
        errors = self._record_detector_failures(fresh_error, size_error)
        failures = tuple(error.recorded() for error in errors)
        return TradeEvidence(evaluated_at, self._detector_config, wallet, size, failures)

    def _record_detector_failures(self, *errors: DetectorError | None) -> tuple[DetectorError, ...]:
        """Count each failed detector and expose the failure in operational state."""
        failed = tuple(error for error in errors if error is not None)
        for error in failed:
            self._stats.errors += 1
            self._stats.last_error = redact_text(error.message)
        return failed

    async def _persist_detector_failure_skip(
        self, bundle: SignalBundle, evidence: TradeEvidence, installed: _InstalledComponents
    ) -> None:
        """Persist why no evidence exists when detector failure is the only outcome.

        Without this row, a trade whose detectors all failed (or whose only working
        detector found nothing) would leave no durable explanation for the absent
        evidence. The skip row is never dispatched.
        """
        if not self._settings.detector.persist_assessments:
            return
        assessment = RiskAssessment(
            trade_event=bundle.trade_event,
            wallet_address=bundle.wallet_address,
            market_id=bundle.market_id,
            delivery_disposition="detector_failure",
            dry_run=self._dry_run,
            # The skip row was produced under the active configuration even though
            # no score was computed; record it so the row stays self-describing.
            scoring_config=installed.current.risk_scorer.scoring_config,
            # No evaluation evidence (contracts/scoring-config.md section 4): failures only.
            evidence=_failures_only(evidence),
        )
        await self._persist_assessment(assessment)

    async def _persist_wallet_and_funding(self, signal: FreshWalletSignal) -> None:
        """Persist wallet profile and funding transfers to Postgres.

        Called when a fresh wallet signal is detected. Upserts the wallet
        profile and traces/inserts any funding transfers found on-chain.

        Args:
            signal: The fresh wallet signal containing the wallet profile.
        """
        if not self._db_manager:
            return

        profile = signal.wallet_profile
        address = profile.address

        try:
            async with self._db_manager.get_async_session() as session:
                wallet_repo = WalletRepository(session)
                dto = WalletProfileDTO(
                    address=address,
                    nonce=profile.nonce,
                    first_seen_at=profile.first_seen,
                    is_fresh=profile.is_fresh,
                    matic_balance=profile.matic_balance,
                    usdc_balance=profile.usdc_balance,
                    analyzed_at=profile.analyzed_at,
                )
                await wallet_repo.upsert(dto)
                funding_transfer_count = await self._persist_funding_transfers(session, address)
                logger.debug(
                    "Persisted wallet profile and %d funding transfers for %s",
                    funding_transfer_count,
                    address[:10] + "...",
                )
        except Exception as e:
            logger.warning(
                "Failed to persist wallet/funding data for %s: %s",
                address,
                redact_exception_message(e),
            )

    async def _persist_funding_transfers(self, session: AsyncSession, address: str) -> int:
        if not self._funding_tracer:
            return 0
        chain = await self._funding_tracer.trace(address)
        if not chain.chain:
            return 0
        funding_repo = FundingRepository(session)
        funding_dtos = [
            FundingTransferDTO(
                from_address=t.from_address,
                to_address=t.to_address,
                amount=t.amount,
                token=t.token,
                tx_hash=t.tx_hash,
                block_number=t.block_number,
                timestamp=t.timestamp,
            )
            for t in chain.chain
        ]
        await funding_repo.insert_many(funding_dtos)
        return len(chain.chain)

    async def _detect_fresh_wallet(
        self, trade: TradeEvent, evaluated_at: datetime, installed: _InstalledComponents
    ) -> tuple[WalletEvaluation | None, DetectorError | None]:
        """Run fresh wallet detection; a failure is returned so it can be counted."""
        detector = installed.current.fresh_wallet_detector
        evaluation = detector.evaluate(trade, evaluated_at=evaluated_at)
        return await _run_detector("fresh_wallet", trade, evaluation)

    async def _detect_size_anomaly(
        self, trade: TradeEvent, evaluated_at: datetime, installed: _InstalledComponents
    ) -> tuple[SizeEvaluation | None, DetectorError | None]:
        """Run size anomaly detection; a failure is returned so it can be counted."""
        detector = installed.current.size_anomaly_detector
        evaluation = detector.evaluate(trade, evaluated_at=evaluated_at)
        return await _run_detector("size_anomaly", trade, evaluation)

    async def _dispatch_alert(
        self,
        formatted_alert: FormattedAlert,
        assessment: RiskAssessment,
        installed: _InstalledComponents,
    ) -> DispatchResult:
        dispatcher = installed.current.alert_dispatcher
        result = await dispatcher.dispatch(formatted_alert, assessment=assessment)
        if result.all_succeeded:
            self._stats.alerts_sent += 1
            logger.info(
                "Alert sent successfully: wallet=%s, score=%.2f",
                assessment.wallet_address[:10] + "...",
                assessment.weighted_score,
            )
        elif not self._dry_run and result.disposition != "duplicate":
            logger.warning(
                "Alert dispatch disposition=%s (%d/%d succeeded)",
                result.disposition,
                result.success_count,
                result.success_count + result.failure_count,
            )
        return result

    async def _send_or_dry_run_alert(
        self, assessment: RiskAssessment, installed: _InstalledComponents
    ) -> DispatchResult:
        formatted_alert = installed.current.alert_formatter.format(assessment)
        if self._dry_run:
            logger.info(
                "[DRY RUN] Would send alert: wallet=%s, score=%.2f",
                assessment.wallet_address[:10] + "...",
                assessment.weighted_score,
            )
        return await self._dispatch_alert(formatted_alert, assessment, installed)

    async def _handle_below_threshold(
        self, bundle: SignalBundle, assessment: RiskAssessment
    ) -> None:
        assessment = dataclasses.replace(
            assessment,
            delivery_disposition="below_threshold",
            dry_run=self._dry_run,
        )
        if self._settings.detector.persist_assessments:
            await self._persist_assessment(assessment)
        logger.debug(
            "Trade %s below alert threshold (score=%.2f)",
            bundle.trade_event.trade_id,
            assessment.weighted_score,
        )

    async def _handle_above_threshold(
        self, assessment: RiskAssessment, installed: _InstalledComponents
    ) -> None:
        """Persist the qualifying assessment before any delivery work, then record the outcome.

        A formatter or dispatch failure — or termination after an external delivery —
        between scoring and persistence must not lose the durable research record
        (FR-012). The pending row carries the ``unrecorded`` disposition until the
        delivery outcome is known; an initial persistence failure stays observable
        and never blocks the authorized delivery attempt (FR-013).
        """
        persist = self._settings.detector.persist_assessments
        pending = dataclasses.replace(assessment, dry_run=self._dry_run)
        pending_written = persist and await self._persist_assessment(pending)
        result = await self._send_or_dry_run_alert(assessment, installed)
        channels_str = json.dumps(result.channel_statuses) if result.channel_statuses else None
        final = dataclasses.replace(
            pending,
            delivery_disposition=result.disposition,
            delivery_channels=channels_str,
        )
        if persist:
            await self._record_final_disposition(final, already_written=pending_written)

    async def _record_final_disposition(
        self, assessment: RiskAssessment, *, already_written: bool
    ) -> None:
        """Update the pending row with the delivery outcome, inserting it if it is missing."""
        if already_written and await self._update_assessment_delivery(assessment):
            return
        await self._persist_assessment(assessment)

    async def _insider_rules(self, trade: TradeEvent) -> list[str]:
        """Named insider rules for one trade (no gate); kept for callers and tests."""
        rules, _ = await self._rules_and_gate(trade, None)
        return rules

    async def _market_info(self, condition_id: str) -> dict[str, object]:
        """Gamma start/end/volume for one market, cached 30 minutes. Empty dict on failure."""
        now = time.monotonic()
        hit = self._market_meta.get(condition_id)
        if hit and now - hit[0] < 1800:
            return hit[1]
        info: dict[str, object] = {}
        try:
            if self._history_client is None:
                self._history_client = httpx.AsyncClient(timeout=15.0)
            resp = await self._history_client.get(
                "https://gamma-api.polymarket.com/markets",
                params={"condition_ids": condition_id, "closed": "true"},
            )
            rows = resp.json() if resp.status_code == 200 else []
            if not rows:
                resp = await self._history_client.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={"condition_ids": condition_id},
                )
                rows = resp.json() if resp.status_code == 200 else []
            if rows:
                m = rows[0]
                info = {
                    "game_start": _parse_ts(m.get("gameStartTime")),
                    "end": _parse_ts(m.get("endDate")),
                    "volume": float(m.get("volume") or 0),
                    "question": m.get("question") or "",
                }
        except Exception as e:
            logger.debug("gamma market lookup failed for %s: %s", condition_id[:10], e)
        if len(self._market_meta) > 20000:
            self._market_meta.clear()
        self._market_meta[condition_id] = (now, info)
        return info

    async def _rules_and_gate(
        self, trade: TradeEvent, bundle: SignalBundle | None
    ) -> tuple[list[str], bool]:
        """Rules, in-play check, and learned-model gate for one trade (local fork).

        Returns (fired rules, suppress). ``suppress`` means: score and persist the trade but
        never alert on it, because the game was already live or the promoted model says the
        wallet has no edge. Every evaluated trade is logged with its features for training.
        """
        if os.environ.get("INSIDER_RULES", "") != "1" or trade.side != "BUY":
            return [], False
        notional = trade.price * trade.size
        watch = is_watch_market(trade.event_title, trade.market_slug, trade.event_slug)
        base = RuleThresholds()
        floor = widened(base) if watch else base
        if notional < floor.min_usdc or trade.price >= floor.max_price:
            return [], False
        trade_ts = int(trade.timestamp.timestamp())
        info = await self._market_info(trade.market_id.lower())
        start = info.get("game_start")
        in_play = isinstance(start, float) and trade_ts >= start
        try:
            history = await self._wallet_history(trade.wallet_address)
        except Exception as e:
            self._record_processing_error(
                f"wallet history failed for {trade.wallet_address[:10]}: "
                f"{redact_exception_message(e)}"
            )
            return [], in_play
        features = features_for(
            history,
            condition_id=trade.market_id.lower(),
            trade_ts=int(trade.timestamp.timestamp()),
            notional_usdc=notional,
            price=trade.price,
            side=trade.side,
            event_slug=trade.event_slug,
        )
        fired = evaluate_tiered(features, watch, base)
        active = is_active_trader(features, base)
        if self._fresh_cluster.observe(
            trade.market_id.lower(),
            trade.outcome,
            trade.wallet_address.lower(),
            trade_ts,
            notional,
            features.wallet_age_hours,  # C1 keeps active wallets; in-play handles live games
        ):
            fired.append("C1_fresh_cluster")
        row = _feature_row(trade, features, fired, info, in_play, watch, active, bundle)
        if active:  # never drop a rule silently: the log says what activity switched off
            row["active_dropped"] = dropped_for_activity(features, base)
        verdict = self._learned.score(row)
        suppress = in_play
        if verdict is not None:
            row["ml_p_win"] = round(verdict.p_win, 4)
            row["ml_edge"] = round(verdict.edge, 4)
            row["ml_version"] = verdict.version
            if verdict.promoted and not in_play:
                gate = float(os.environ.get("ML_GATE_MIN_EDGE", "0.0"))
                lift = float(os.environ.get("ML_ALERT_EDGE", "0.15"))
                if fired and verdict.edge < gate:
                    row["ml_suppressed"] = True
                    suppress = True
                elif (
                    not fired
                    and verdict.edge >= lift
                    and notional >= Decimal(os.environ.get("ML_ALERT_USDC", "2000"))
                ):
                    fired = ["M1_model_edge"]
        append_features(row)
        if in_play:
            return [], True
        if fired and not suppress:
            logger.info(
                "insider rule fired: %s wallet=%s market=%s usdc=%s price=%s age_h=%.1f mkts=%d",
                ",".join(fired),
                trade.wallet_address,
                trade.market_id,
                round(notional),
                trade.price,
                features.wallet_age_hours or 0.0,
                features.prior_markets,
            )
            _append_rule_hit(trade, fired, features, row)
        return fired, suppress

    async def _wallet_history(self, address: str) -> WalletHistory:
        """Wallet history with a 2-minute in-process cache (a wallet's fills arrive in bursts)."""
        now = time.monotonic()
        cache = self._history_cache
        hit = cache.get(address)
        if hit and now - hit[0] < 120:
            return hit[1]
        if self._history_client is None:
            self._history_client = httpx.AsyncClient(timeout=15.0)
        history = await fetch_wallet_history(address, client=self._history_client, max_pages=2)
        if len(cache) > 5000:
            cache.clear()
        cache[address] = (now, history)
        return history

    async def _score_and_alert(
        self,
        bundle: SignalBundle,
        installed: _InstalledComponents,
        rules: list[str] | None = None,
        *,
        suppress: bool = False,
    ) -> None:
        """Score signals, persist the assessment, and send alert if above threshold."""
        assessment = await installed.current.risk_scorer.assess(bundle)
        if suppress:
            # Local fork: live game or no learned edge. Keep the row, never alert.
            assessment = dataclasses.replace(assessment, should_alert=False)
        elif rules:
            threshold = float(self._settings.detector.alert_threshold)
            assessment = dataclasses.replace(
                assessment,
                should_alert=True,
                weighted_score=max(assessment.weighted_score, threshold),
                insider_rules=tuple(rules),
            )
        if not assessment.should_alert:
            await self._handle_below_threshold(bundle, assessment)
            return

        await self._handle_above_threshold(assessment, installed)

    async def _update_assessment_delivery(self, assessment: RiskAssessment) -> bool:
        """Record the delivery outcome on the pending row. Best-effort; never raises."""
        if not self._db_manager:
            return False
        try:
            async with self._db_manager.get_async_session() as session:
                repo = RiskAssessmentRepository(session)
                return await repo.update_delivery(
                    assessment.assessment_id,
                    delivery_disposition=assessment.delivery_disposition,
                    delivery_channels=assessment.delivery_channels,
                    dry_run=assessment.dry_run,
                )
        except Exception as e:
            self._record_processing_error(
                f"Failed to record delivery outcome for assessment {assessment.assessment_id}:"
                f" {redact_exception_message(e)}"
            )
            return False

    async def _persist_assessment(self, assessment: RiskAssessment) -> bool:
        """Write the assessment row. Best-effort; never raises.

        Returns True when the row is durably written. A failure is observable
        (counted, surfaced at /health) without blocking delivery (FR-013). A
        cancellation mid-write is re-raised only after the write completes (bounded).
        """
        if not self._db_manager:
            return False
        dto = assessment_dto(
            assessment,
            alert_threshold=self._settings.detector.alert_threshold,
            detector_config=self._detector_config,
        )
        try:
            await insert_despite_cancellation(self._db_manager, dto, self._record_processing_error)
        except Exception as e:
            self._record_processing_error(
                f"Failed to persist risk assessment {assessment.assessment_id}:"
                f" {redact_exception_message(e)}"
            )
            return False
        return True

    async def run(self) -> None:
        """Start the pipeline and run until interrupted.

        This is a convenience method that starts the pipeline and
        blocks until a stop signal is received.

        Example:
            ```python
            pipeline = Pipeline()
            try:
                await pipeline.run()
            except KeyboardInterrupt:
                pass
            ```
        """
        await self.start()

        try:
            if self._stop_event:
                await self._stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def __aenter__(self) -> Pipeline:
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        """Async context manager exit."""
        await self.stop()


RULE_HITS_PATH = os.path.expanduser(
    os.environ.get("RULE_HITS_PATH", "~/.polywatch/rule_hits.jsonl")
)


def _parse_ts(raw: object) -> float | None:
    """Gamma timestamps come as ISO with Z, or '2026-11-05 01:30:00+00'."""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip().replace(" ", "T", 1).replace("Z", "+00:00")
    if text.endswith("+00"):
        text += ":00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _feature_row(
    trade: TradeEvent,
    f: TradeFeatures,
    fired: list[str],
    info: dict[str, object],
    in_play: bool,
    watch: bool,
    active: bool,
    bundle: SignalBundle | None,
) -> dict[str, object]:
    """Everything the learner trains on, for one evaluated BUY."""
    ts = trade.timestamp.timestamp()
    start, end = info.get("game_start"), info.get("end")
    size = bundle.size_anomaly_signal if bundle else None
    fresh = bundle.fresh_wallet_signal if bundle else None
    return {
        "ts": int(ts),
        "trade_id": trade.trade_id,
        "wallet": trade.wallet_address.lower(),
        "market": trade.market_id.lower(),
        "outcome": trade.outcome,
        "outcome_index": trade.outcome_index,
        "title": info.get("question") or trade.event_title,
        "slug": trade.market_slug or trade.event_slug,
        "price": float(trade.price),
        "usdc": round(float(trade.price * trade.size), 2),
        "age_h": round(f.wallet_age_hours or 0.0, 2),
        "prior_markets": f.prior_markets,
        "prior_trades": f.prior_trades,
        "market_share": float(f.market_share) if f.market_share is not None else None,
        "market_cum": float(f.market_cumulative_usdc or 0),
        "longshot_markets": f.longshot_markets,
        "longshot_wins": f.longshot_wins,
        "event_markets": f.event_markets,
        "event_share": float(f.event_share) if f.event_share is not None else None,
        "in_play": int(in_play),
        "hours_to_start": round((start - ts) / 3600, 2) if isinstance(start, float) else None,
        "hours_to_end": round((end - ts) / 3600, 2) if isinstance(end, float) else None,
        "market_volume": info.get("volume"),
        "niche": int(bool(size and size.is_niche_market)),
        "size_conf": float(size.confidence) if size else 0.0,
        "fresh_conf": float(fresh.confidence) if fresh else 0.0,
        "watch": int(watch),
        "active": int(active),
        "rules": list(fired),
    }


def _append_rule_hit(
    trade: TradeEvent,
    fired: list[str],
    f: TradeFeatures,
    extra: dict[str, object] | None = None,
) -> None:
    """One JSON line per fired trade, for `polywatch rules`, SMS alerts and later tuning."""
    if os.environ.get("PYTEST_CURRENT_TEST"):  # tests never write the live alert log
        return
    extra = extra or {}
    row = {
        "ml_edge": extra.get("ml_edge"),
        "ml_p_win": extra.get("ml_p_win"),
        "hours_to_start": extra.get("hours_to_start"),
        "prior_trades": f.prior_trades,
        "active_dropped": extra.get("active_dropped"),
        "trade_id": trade.trade_id,
        "ts": trade.timestamp.isoformat(),
        "seen": datetime.now(UTC).isoformat(timespec="seconds"),
        "lag_s": round(datetime.now(UTC).timestamp() - trade.timestamp.timestamp(), 1),
        "rules": fired,
        "wallet": trade.wallet_address,
        "market": trade.market_id,
        "slug": trade.market_slug or trade.event_slug,
        "title": trade.event_title,
        "outcome": trade.outcome,
        "price": str(trade.price),
        "usdc": str(round(trade.price * trade.size, 2)),
        "age_h": round(f.wallet_age_hours or 0.0, 1),
        "prior_markets": f.prior_markets,
        "market_share": str(round(f.market_share, 2)) if f.market_share is not None else None,
    }
    try:
        os.makedirs(os.path.dirname(RULE_HITS_PATH), exist_ok=True)
        with open(RULE_HITS_PATH, "a") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError as e:
        logger.warning("rule hit log write failed: %s", e)
