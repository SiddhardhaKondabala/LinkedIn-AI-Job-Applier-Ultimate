"""This module is used to run the LinkedIn/Indeed bot"""

import asyncio
import os
import traceback
from pathlib import Path
from threading import Lock

import dotenv
from pypdf import PdfReader

# TODO: Create a tutorial video for the bot


# Try to import pynput for keyboard control (optional, not available in Docker)
try:
    from pynput import keyboard as pynput_kb

    PYNPUT_AVAILABLE = True
except (ImportError, Exception):
    PYNPUT_AVAILABLE = False
    pynput_kb = None

from config.app_config import JOB_SITE, RESTART_EVERY_DAY
from config.constants import (
    BROWSER_STORAGE_STATE,
    RESUME_DIR,
    RESUME_TEXT_TEMPLATE_FILE,
    SEARCH_CONFIG_FILE,
)
from config.logger_config import logger
from src.dashboard.runtime import StopRequested, emit_event, get_control_state, update_control_state

if JOB_SITE == "indeed":
    from src.job_manager.indeed.authenticator_indeed import IndeedAuthenticator as Authenticator
    from src.job_manager.indeed.job_manager_indeed import IndeedJobManager as LinkedInJobManager
    from src.job_manager.indeed.search_customizer_indeed import (
        IndeedSearchCustomizer as SearchCustomizer,
    )
else:
    from src.job_manager.linkedin.authenticator_linkedin import (
        LinkedInAuthenticator as Authenticator,
    )
    from src.job_manager.linkedin.job_manager_linkedin import LinkedInJobManager
    from src.job_manager.linkedin.search_customizer_linkedin import SearchCustomizer

from src.job_manager.bot_facade import BotFacade
from src.job_manager.resume_anonymizer import ResumeAnonymizer
from src.llm.apply_agent import ApplyAgent
from src.llm.llm_manager import GPTAnswerer
from src.pydantic_models.config_models import SearchConfig, Secrets
from src.pydantic_models.prompt_models import ResumeStructure
from src.resume_builder.resume_generator import ResumeGenerator
from src.resume_builder.resume_manager import ResumeManager
from src.resume_builder.style_manager import StyleManager
from src.utils.browser_utils import create_playwright_browser, save_browser_session, stop_tracing
from src.utils.runtime_control import (
    ShutdownState,
    register_shutdown_handlers,
    runtime_controller,
    sleep_with_shutdown,
)
from src.utils.utils import (
    get_ready_made_resume,
    load_yaml_file,
    save_yaml_file,
    validate_and_prompt_resume_completion,
)
from src.integration.cli import parse_queue_args, queue_config_from_args
from src.integration.queue_loader import load_queue, queue_min_score
from src.integration.queue_runner import process_queue
from src.integration.artifacts import preflight_artifacts, resolve_job_artifacts
from src.integration.executor_adapter import make_apply_fn
from src.integration.result_writer import ResultWriter
from src.integration.claims import ClaimStore, claimant_from_store

# Create necessary directories if they don't exist
os.makedirs(RESUME_DIR, exist_ok=True)

# Resume file paths
RESUME_STRUCTURED_FILE = Path(RESUME_DIR) / "structured_resume.yaml"
RESUME_TEXT_FILE = Path(RESUME_DIR) / "resume_text.txt"

READY_MADE_RESUME = get_ready_made_resume()

# Global pause state for keyboard control
paused = False
pause_lock = Lock()
ctrl_pressed = False
last_dashboard_pause_state = False


class ConfigError(Exception):
    pass


class ConfigValidator:
    """Class for validating configuration settings"""

    def validate_search_config(self, config_yaml_path: Path) -> dict:
        """Validate LinkedIn search configuration settings"""
        try:
            parameters = load_yaml_file(config_yaml_path)
            parameters = SearchConfig(**parameters)
            logger.debug("LinkedIn search config loaded successfully.")
        except Exception as e:
            raise ConfigError(f"LinkedIn configuration validation error: {str(e)}")
        # LinkedIn resolves only one location per search; Indeed runs a search per location
        if JOB_SITE == "linkedin" and len(parameters.locations) > 1:
            raise ConfigError(
                f"LinkedIn supports one location per run, found {len(parameters.locations)}: "
                f"{', '.join(parameters.locations)}"
            )
        return parameters.model_dump()

    @staticmethod
    def validate_secrets() -> dict:
        """Check for required secret keys based on active JOB_SITE"""
        secrets = {**dotenv.dotenv_values(".env")}
        try:
            if JOB_SITE == "indeed":
                required_keys = ["indeed_email"]
            else:
                required_keys = ["linkedin_email", "linkedin_password"]

            missing_keys = [key for key in required_keys if not secrets.get(key)]
            if missing_keys:
                raise ValueError(f"Missing required keys: {', '.join(missing_keys)}")

            secrets_config = Secrets(**secrets)
            logger.debug(f"{JOB_SITE} secrets validated successfully.")
            return secrets_config.model_dump()
        except Exception as e:
            raise ConfigError(f"Secrets validation error: {str(e)}")

    @staticmethod
    def validate_resume_text(resume_file: Path) -> str:
        """Check for resume file"""
        try:
            with open(resume_file, "r", encoding="utf-8") as f:
                resume_text = f.read()
                if not resume_text:
                    raise ConfigError("Resume not found")
                return resume_text
        except FileNotFoundError:
            return ""
        except Exception as e:
            raise ConfigError(f"Resume validation error: {str(e)}")

    @staticmethod
    def validate_resume_structured(resume_structured_file: Path) -> dict:
        """Check for structured resume file"""
        try:
            resume_structured = load_yaml_file(resume_structured_file)
            resume_structured = ResumeStructure(**resume_structured)
            return resume_structured.model_dump()
        except Exception as e:
            if str(e).startswith("File not found"):
                logger.warning("Resume template not found, creating new one")
                return {}
            raise ConfigError(f"Structured resume validation error: {str(e)}")


def generate_resume_text_from_pdf(secrets: dict) -> str:
    """Find a PDF in RESUME_DIR, parse it, and write a formatted resume_text.txt using the LLM."""
    pdf_files = list(Path(RESUME_DIR).glob("*.pdf"))
    if not pdf_files:
        return ""

    pdf_path = pdf_files[0]
    logger.info(f"Generating resume_text.txt from {pdf_path.name}")

    reader = PdfReader(pdf_path)
    raw_text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    if not raw_text:
        logger.warning(f"Could not extract text from {pdf_path.name}")
        return ""

    template = Path(RESUME_TEXT_TEMPLATE_FILE).read_text(encoding="utf-8")

    llm_answerer = GPTAnswerer(
        secrets.get("llm_api_key"), secrets.get("llm_proxy"), secrets.get("llm_api_url")
    )
    resume_text = llm_answerer.generate_resume_text(raw_text, template)

    RESUME_TEXT_FILE.write_text(resume_text, encoding="utf-8")
    logger.info(f"resume_text.txt generated and saved to {RESUME_TEXT_FILE}")
    return resume_text


def on_press(key):
    """Handle key press events"""
    if not PYNPUT_AVAILABLE:
        return

    global paused, ctrl_pressed
    try:
        # Track Ctrl key state
        if key in (pynput_kb.Key.ctrl_l, pynput_kb.Key.ctrl_r):
            ctrl_pressed = True
        # Check for 'x' key when Ctrl is pressed
        elif hasattr(key, "char") and key.char == "x" and ctrl_pressed:
            with pause_lock:
                paused = not paused
                if paused:
                    logger.warning("⏸️  PAUSED - Press Ctrl+X to continue")
                    emit_event("pause_state_changed", "Keyboard pause requested", paused=True)
                else:
                    logger.info("▶️  RESUMED")
                    emit_event("pause_state_changed", "Keyboard resume requested", paused=False)
    except AttributeError:
        pass


def on_release(key):
    """Handle key release events"""
    if not PYNPUT_AVAILABLE:
        return

    global ctrl_pressed
    # Reset Ctrl key state
    if key in (pynput_kb.Key.ctrl_l, pynput_kb.Key.ctrl_r):
        ctrl_pressed = False


def start_keyboard_listener():
    """Start keyboard listener in background thread"""
    if not PYNPUT_AVAILABLE:
        logger.info("Keyboard control disabled (pynput not available - Docker/headless mode)")
        return

    try:
        listener = pynput_kb.Listener(on_press=on_press, on_release=on_release)
        listener.daemon = True
        listener.start()
        logger.info("Keyboard listener started - Press Ctrl+X to pause/resume")
    except Exception as e:
        logger.warning(f"Could not start keyboard listener: {e}")


async def check_pause():
    """Check if execution is paused and wait if needed"""
    global last_dashboard_pause_state, paused

    control = get_control_state() if os.environ.get("DASHBOARD_RUN_ID") else {}
    effective_paused = paused or control.get("pause_requested", False)

    if control.get("stop_requested"):
        raise StopRequested("Dashboard requested stop")

    if effective_paused != last_dashboard_pause_state:
        emit_event(
            "pause_state_changed",
            f"Execution {'paused' if effective_paused else 'resumed'}",
            paused=effective_paused,
            source="dashboard" if control.get("pause_requested") and not paused else "keyboard",
        )
        last_dashboard_pause_state = effective_paused

    while paused or (
        os.environ.get("DASHBOARD_RUN_ID") and get_control_state().get("pause_requested", False)
    ):
        if os.environ.get("DASHBOARD_RUN_ID") and get_control_state().get("stop_requested"):
            raise StopRequested("Dashboard requested stop")
        await asyncio.sleep(0.5)

    if last_dashboard_pause_state:
        emit_event("pause_state_changed", "Execution resumed", paused=False)
        last_dashboard_pause_state = False


async def create_and_run_bot(
    search_config: dict,
    secrets: dict,
    resume_text: str,
    resume_structured: dict,
    queue_run: dict = None,
):
    """Start LinkedIn bot (async). When queue_run is given, process a career-ops
    approved queue instead of searching."""
    logger.info("Initializing LinkedIn bot...")
    # Fresh shutdown state per run (matters when RESTART_EVERY_DAY loops runs)
    runtime_controller.set_shutdown_state(ShutdownState.RUNNING)
    emit_event(
        "run_started",
        "LinkedIn bot run started",
        positions=search_config.get("positions", []),
        locations=search_config.get("locations", []),
    )

    # Initialize browser Playwright based on configuration
    try:
        browser, context, page = await create_playwright_browser()
        # Local runtime patch: detect manual browser closure and recover cleanly.
        logger.info("Playwright browser initialized successfully")
        emit_event("browser_initialized", "Playwright browser initialized")

    except Exception as e:
        runtime_controller.finish_run()
        logger.error(f"Browser initialization error: {e}")
        emit_event("run_failed", "Browser initialization failed", error=str(e))
        raise RuntimeError(f"Failed to initialize browser: {e}")

    try:
        # Resolve credentials based on active site
        if JOB_SITE == "indeed":
            site_email = secrets["indeed_email"]
            site_password = None
        else:
            site_email = secrets["linkedin_email"]
            site_password = secrets["linkedin_password"]

        # Initialize authenticator
        authenticator = Authenticator(page)
        authenticator.set_parameters(site_email, site_password)

        # Attempt login
        login_success = await authenticator.start()
        if login_success:
            await save_browser_session(context)
            logger.info("Successfully logged into LinkedIn!")
            logger.info("LinkedIn bot ready to work")
            emit_event("login_success", "LinkedIn login succeeded")
        else:
            logger.error("Failed to log into LinkedIn")
            emit_event("run_failed", "LinkedIn login failed")
            return False

        # Set GPT answerer
        llm_api_key = secrets.get("llm_api_key")
        llm_proxy = secrets.get("llm_proxy")
        llm_api_url = secrets.get("llm_api_url")
        llm_answerer_component = GPTAnswerer(llm_api_key, llm_proxy, llm_api_url)
        llm_agent_component = ApplyAgent(
            llm_api_key, BROWSER_STORAGE_STATE, llm_api_url, site_email
        )

        linkedin_email = site_email  # kept for LinkedInJobManager constructor compatibility

        if not resume_structured:
            resume_structured = llm_answerer_component.parse_resume(resume_text)
            resume_structured = ResumeStructure(**resume_structured).model_dump()
            save_yaml_file(RESUME_STRUCTURED_FILE, resume_structured)

        # Set resume anonymizer and anonymize the resume information
        resume_anonymizer = ResumeAnonymizer(resume_structured)
        resume_anonymizer.anonymize_personal_information()
        resume_structured = resume_anonymizer.resume_anonymized
        resume_text_anonymized = resume_anonymizer.anonymize_text(resume_text)

        # Set GPT resume generator
        style_manager = StyleManager()
        resume_generator = ResumeGenerator(llm_answerer_component, resume_anonymizer)
        resume_generator_manager = ResumeManager(llm_api_key, style_manager, resume_generator)

        resume_ready_made = READY_MADE_RESUME is not None and READY_MADE_RESUME.resolve().is_file()
        if not resume_ready_made:
            resume_generator_manager.choose_style()

        # Set search component
        search_component = SearchCustomizer(page)

        # Set apply component
        apply_component = LinkedInJobManager(
            page, linkedin_email, resume_anonymizer, search_component
        )

        # Set bot facade
        bot = BotFacade(resume_anonymizer, search_component, apply_component, llm_agent_component)
        bot.set_parameters(search_config)
        bot.set_pause_checker(check_pause)

        # Queue mode: apply to the career-ops approved queue, no search / no
        # daily-window check / no resume prompt.
        if queue_run is not None:
            await _run_queue(
                apply_component,
                llm_answerer_component,
                llm_agent_component,
                resume_structured,
                resume_text_anonymized,
                resume_generator_manager,
                resume_ready_made,
                search_config,
                queue_run,
                check_pause,
            )
            emit_event("run_completed", "Queue-mode run completed successfully")
            return True

        # Check if the last search was less than a day ago (LinkedIn only)
        if (
            RESTART_EVERY_DAY
            and JOB_SITE == "linkedin"
            and not apply_component.check_the_last_search_time()
        ):
            logger.warning(
                "Last search was less than a day ago, finishing work. If you want to restart the search, delete the file data/output/last_run.yaml file"
            )
            emit_event(
                "run_stopped", "Run skipped because the daily restart window is still active"
            )
            return True

        # Validate structured resume and prompt user if needed (skip when launched from dashboard)
        if not os.environ.get("DASHBOARD_RUN_ID") and not validate_and_prompt_resume_completion(
            resume_structured, RESUME_STRUCTURED_FILE, RESUME_TEXT_FILE
        ):
            logger.info("User chose to exit and complete resume information")
            emit_event("run_stopped", "Run stopped because resume validation was not accepted")
            return False

        await bot.set_search_parameters(search_config)
        bot.set_answerer_and_agent(llm_answerer_component, llm_agent_component, search_config)
        bot.set_resume(resume_structured, resume_text, resume_text_anonymized)
        if not resume_ready_made:
            bot.set_resume_generator(resume_generator_manager)
        await bot.start_apply()
        emit_event("run_completed", "LinkedIn bot run completed successfully")

    finally:
        # Cleanup browser resources
        logger.info("Cleaning up browser resources...")
        runtime_controller.set_shutdown_state(ShutdownState.CLEANUP)
        try:
            if context is not None:
                await save_browser_session(context)
                await stop_tracing(context)
            # Close Playwright browser (browser is None when using persistent context)
            if browser is not None:
                await browser.close()
            elif context is not None:
                await context.close()
            logger.info("Playwright browser closed")
            emit_event("browser_closed", "Playwright browser closed")

        except Exception as e:
            logger.warning(f"Error during browser cleanup: {e}")
        finally:
            runtime_controller.set_shutdown_state(ShutdownState.DONE)
            # Local runtime patch: release any pending shutdown handler waits.
            runtime_controller.finish_run()


async def _run_queue(
    apply_component,
    llm_answerer,
    llm_agent,
    resume_structured,
    resume_text_anonymized,
    resume_generator_manager,
    resume_ready_made,
    search_config,
    queue_run,
    pause_checker,
):
    """Wire the apply component and run the approved queue (no search)."""
    # Queue mode never searches, so wire the apply component directly instead of
    # going through the search-first BotFacade ordering.
    llm_answerer.set_search_parameters(search_config)
    llm_answerer.set_resume(resume_structured, resume_text_anonymized)
    apply_component.set_answerer_and_agent(llm_answerer, llm_agent)
    apply_component.set_resume(resume_structured)
    apply_component.set_pause_checker(pause_checker)
    if not resume_ready_made:
        apply_component.set_resume_generator_manager(resume_generator_manager)

    # Write each result the moment its job finishes, so an interruption still
    # leaves durable records; the writer is idempotent on (run, job).
    writer = ResultWriter(queue_run["results_path"]) if queue_run.get("results_path") else None

    # Phase 8: claim each job before applying so a concurrent or resumed run
    # never double-applies. Claims live next to the results file; without a
    # results path there is nowhere durable to keep them, so claiming is off.
    claim = release_claim = None
    if queue_run.get("results_path"):
        claim_store = ClaimStore(str(Path(queue_run["results_path"]).parent / "claims"))
        claim, release_claim = claimant_from_store(claim_store)

    apply_fn = make_apply_fn(apply_component, allow_external=queue_run["allow_external"])
    results = await process_queue(
        queue_run["jobs"],
        apply_fn,
        queue_run["config"],
        pause_checker=pause_checker,
        on_result=(writer.append if writer else None),
        verify_artifacts=resolve_job_artifacts,  # Phase 6 per-job integrity gate
        claim=claim,  # Phase 8 double-apply guard
        release_claim=release_claim,
        reraise=(StopRequested,),  # a stop must halt the run, not become a failed result
    )
    logger.info(
        f"[queue] Processed {len(results)} job(s); results -> {queue_run.get('results_path')}"
    )
    return results


async def run_queue_mode(args) -> bool:
    """Queue mode: apply to a career-ops approved queue instead of searching."""
    config_validator = ConfigValidator()
    secrets = config_validator.validate_secrets()
    search_config = config_validator.validate_search_config(SEARCH_CONFIG_FILE)
    resume_text = config_validator.validate_resume_text(RESUME_TEXT_FILE)
    if not resume_text:
        resume_text = generate_resume_text_from_pdf(secrets)
    resume_structured = config_validator.validate_resume_structured(RESUME_STRUCTURED_FILE)
    if not resume_text and not resume_structured:
        raise FileNotFoundError("Resume not found for queue-mode run")

    # Load + validate the queue BEFORE any browser work (fail fast on a bad queue).
    doc, jobs = load_queue(args.approved_jobs)
    config = queue_config_from_args(args, default_min_score=queue_min_score(doc))
    logger.info(
        f"[queue] Loaded {len(jobs)} approved job(s); min_score={config.min_score}, "
        f"submit={config.should_submit}, dry_run={config.dry_run}"
    )

    # Phase 6: verify every approved artifact (resume/cover letter exists and
    # still matches its SHA-256) BEFORE launching the browser, so a missing or
    # swapped file is reported up front. Problem jobs still get a durable result
    # from the per-job runner gate, so the good jobs in the queue proceed.
    problems = preflight_artifacts(jobs)
    for job, err in problems:
        logger.warning(
            f"[queue] Artifact preflight failed for {job.integration_id} "
            f"({job.company} — {job.role}): {err}"
        )
    if problems:
        logger.warning(
            f"[queue] {len(problems)}/{len(jobs)} job(s) have artifact problems; "
            "they will be recorded for manual review and skipped during the run."
        )

    queue_run = {
        "jobs": jobs,
        "config": config,
        "results_path": args.results,
        "allow_external": bool(args.allow_external),
    }
    return await create_and_run_bot(
        search_config, secrets, resume_text, resume_structured, queue_run=queue_run
    )


def main() -> None:
    args = parse_queue_args()

    # Queue mode runs once (no daily restart loop).
    if args.approved_jobs:
        register_shutdown_handlers()
        start_keyboard_listener()
        if not os.environ.get("DASHBOARD_RUN_ID"):
            update_control_state(stop_requested=False, pause_requested=False)
        asyncio.run(run_queue_mode(args))
        return

    # Start keyboard listener for pause/resume functionality
    # Local runtime patch: register graceful shutdown handlers once at startup.
    register_shutdown_handlers()
    start_keyboard_listener()
    if not os.environ.get("DASHBOARD_RUN_ID"):
        update_control_state(stop_requested=False, pause_requested=False)

    while True:
        should_exit = False
        try:
            # create output folder if it doesn't exist
            data = Path("data")
            output_folder = data / "output"
            output_folder.mkdir(exist_ok=True)
            linkedin_output_folder = output_folder / "linkedin"
            linkedin_output_folder.mkdir(exist_ok=True)
            indeed_output_folder = output_folder / "indeed"
            indeed_output_folder.mkdir(exist_ok=True)

            # validate config files
            config_validator = ConfigValidator()
            secrets = config_validator.validate_secrets()
            search_config = config_validator.validate_search_config(SEARCH_CONFIG_FILE)
            resume_text = config_validator.validate_resume_text(RESUME_TEXT_FILE)
            if not resume_text:
                resume_text = generate_resume_text_from_pdf(secrets)
            resume_structured = config_validator.validate_resume_structured(RESUME_STRUCTURED_FILE)

            if not resume_text and not resume_structured:
                raise FileNotFoundError(
                    f"Can't find neither resume text file {RESUME_TEXT_FILE} nor resume structured file {RESUME_STRUCTURED_FILE}"
                )

            logger.info(f"Starting {JOB_SITE.capitalize()} Job Applier...")
            logger.info(f"Search config loaded with {len(search_config)} parameters")

            asyncio.run(create_and_run_bot(search_config, secrets, resume_text, resume_structured))
            logger.info(f"{JOB_SITE.capitalize()} bot completed successfully")

        except StopRequested as stop_requested:
            logger.warning(str(stop_requested))
            emit_event("run_stopped", "Run stopped gracefully by dashboard")
            should_exit = True

        except StopRequested as stop_requested:
            logger.warning(str(stop_requested))
            emit_event("run_stopped", "Run stopped gracefully by dashboard")
            should_exit = True

        except StopRequested as stop_requested:
            logger.warning(str(stop_requested))
            emit_event("run_stopped", "Run stopped gracefully by dashboard")
            should_exit = True

        except ConfigError as ce:
            logger.error(f"Configuration error: {str(ce)}")
            emit_event("run_failed", "Configuration error", error=str(ce))
        except FileNotFoundError as fnf:
            tb_str = traceback.format_exc()
            logger.error(f"File not found: {str(fnf)}\n{tb_str}")
            emit_event("run_failed", "Required file was not found", error=str(fnf))
        except RuntimeError as re:
            tb_str = traceback.format_exc()
            logger.error(f"Runtime error: {str(re)}\n{tb_str}")
            emit_event("run_failed", "Runtime error", error=str(re))
        except Exception as e:
            tb_str = traceback.format_exc()
            logger.error(f"Unknown error: {str(e)}\n{tb_str}")
            emit_event("run_failed", "Unhandled exception", error=str(e))
        finally:
            logger.info("Program completed")
            # Wait 1 hour total before next run
            if RESTART_EVERY_DAY and not should_exit:
                logger.info("Waiting 1 hour before next run")
                # Local runtime patch: make the daily wait interruptible.
                if not asyncio.run(sleep_with_shutdown(3600)):
                    logger.info("Shutdown requested during wait interval")
                    should_exit = True
            else:
                logger.info("Exiting program")
                should_exit = True

        if should_exit:
            break


if __name__ == "__main__":
    main()
