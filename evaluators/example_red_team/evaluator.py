"""
Generic Green Evaluator (Red Team Orchestrator)

This evaluator dynamically loads and runs multiple scenarios against a defender.
Teams can customize which scenarios to run via evaluator.toml.
"""

import argparse
import asyncio
import importlib.util
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCard, AgentCapabilities, TaskState, Part, TextPart
from a2a.utils import new_agent_text_message
import uvicorn

from agentbeats.green_executor import GreenAgent, GreenExecutor
from agentbeats.models import EvalRequest, EvalResult
from agentbeats.scenario_schema import (
    ScenarioResult,
    GreenEvaluatorResult,
    create_evaluator_result
)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("generic_evaluator")


class GenericGreenEvaluator(GreenAgent):
    """Generic green agent that runs multiple scenarios"""

    def __init__(self, evaluator_name: str, scenario_configs: list[dict[str, Any]]):
        self.evaluator_name = evaluator_name
        self.scenario_configs = scenario_configs
        self._required_roles = ["defender"]
        self._required_config_keys = []

    def validate_request(self, request: EvalRequest) -> tuple[bool, str]:
        """Validate evaluation request"""
        if "defender" not in request.participants:
            return False, "Missing required role: defender"
        return True, "ok"

    async def run_eval(self, req: EvalRequest, updater: TaskUpdater) -> None:
        """Execute all configured scenarios"""
        logger.info(f"Starting {self.evaluator_name} evaluation")

        try:
            defender_url = str(req.participants["defender"])
            start_time = time.time()
            scenario_results = []

            # Run each configured scenario
            for i, scenario_config in enumerate(self.scenario_configs, 1):
                scenario_name = scenario_config["name"]
                scenario_params = scenario_config.get("config", {})

                await updater.update_status(
                    TaskState.working,
                    new_agent_text_message(
                        f"Running scenario {i}/{len(self.scenario_configs)}: {scenario_name}"
                    )
                )

                logger.info(f"Loading scenario: {scenario_name}")

                # Dynamically import scenario
                try:
                    scenario_module = self._load_scenario(scenario_name)
                    run_scenario_func = getattr(scenario_module, "run_scenario")

                    # Run the scenario
                    logger.info(f"Executing scenario: {scenario_name}")
                    scenario_result = await run_scenario_func(
                        defender_url=defender_url,
                        config=scenario_params
                    )

                    scenario_results.append(scenario_result)

                    await updater.update_status(
                        TaskState.working,
                        new_agent_text_message(
                            f"Scenario {scenario_name}: "
                            f"ASR={scenario_result.metrics.asr:.2%}, "
                            f"Baseline={'PASS' if scenario_result.metrics.baseline_correct else 'FAIL'}"
                        )
                    )

                except Exception as e:
                    logger.error(f"Failed to run scenario {scenario_name}: {e}", exc_info=True)
                    await updater.update_status(
                        TaskState.working,
                        new_agent_text_message(f"Scenario {scenario_name}: ERROR - {str(e)}")
                    )
                    # Continue with other scenarios even if one fails
                    continue

            if not scenario_results:
                raise ValueError("No scenarios were successfully executed")

            # Create evaluator result
            evaluator_result = create_evaluator_result(
                evaluator_name=self.evaluator_name,
                defender_name=defender_url.split(":")[-1].replace("/", ""),
                scenario_results=scenario_results,
                timestamp=datetime.now().isoformat(),
                config=req.config
            )

            # Format and display results
            results_text = self._format_results(evaluator_result)
            logger.info("\n" + results_text)

            await updater.update_status(
                TaskState.working,
                new_agent_text_message(f"\n{results_text}")
            )

            # Save results to disk
            results_dir_env = os.getenv("AGENTBEATS_RESULTS_DIR")
            if results_dir_env:
                results_dir = Path(results_dir_env)
                result_filename = f"eval_{self.evaluator_name}_evaluator.json"
            else:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                results_dir = Path("results") / f"run_{timestamp}"
                results_dir.mkdir(parents=True, exist_ok=True)
                result_filename = f"eval_{self.evaluator_name}_evaluator.json"

            result_path = results_dir / result_filename

            with open(result_path, 'w') as f:
                f.write(evaluator_result.model_dump_json(indent=2))

            logger.info(f"Results saved to: {result_path}")

            await updater.update_status(
                TaskState.working,
                new_agent_text_message(f"Results saved to: {result_path}")
            )

            # Send final artifact
            await updater.add_artifact(
                parts=[
                    Part(root=TextPart(text=results_text)),
                    Part(root=TextPart(text=evaluator_result.model_dump_json(indent=2)))
                ],
                name="Green Evaluator Result"
            )

            await updater.update_status(
                TaskState.submitted,
                new_agent_text_message("Evaluation complete")
            )

            # Determine winner
            avg_detection_pct = (1 - evaluator_result.metrics.avg_asr) * 100
            winner = "DEFENDER" if avg_detection_pct > 50 else "ATTACKER"

            # Return result for agentbeats framework
            result = EvalResult(
                winner=winner,
                detail={
                    "evaluator_name": evaluator_result.evaluator_name,
                    "avg_asr": evaluator_result.metrics.avg_asr,
                    "scenario_coverage": evaluator_result.metrics.scenario_coverage,
                    "overall_efficiency": evaluator_result.metrics.overall_efficiency,
                    "baseline_correct_rate": evaluator_result.metrics.baseline_correct_rate,
                    "phase1_score": evaluator_result.phase1_score,
                    "phase2_score": evaluator_result.phase2_score,
                    "total_scenarios": evaluator_result.metrics.total_scenarios,
                    "total_attacks": evaluator_result.metrics.total_attacks,
                    "total_successes": evaluator_result.metrics.total_successes,
                    "total_time": evaluator_result.total_time_elapsed
                }
            )

            # Set result on green agent executor
            if hasattr(updater, '_executor') and hasattr(updater._executor, 'result'):
                updater._executor.result = result

        except Exception as e:
            logger.error(f"Evaluation error: {e}", exc_info=True)
            await updater.update_status(
                TaskState.failed,
                new_agent_text_message(f"Error: {str(e)}")
            )
            raise

    def _load_scenario(self, scenario_name: str):
        """Dynamically load a scenario module with proper package structure"""
        # Ensure project root is in sys.path for package imports
        project_root = Path.cwd()
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))

        # Set up parent packages in sys.modules
        # This is required for relative imports to work

        # 1. Create 'scenarios' package if not exists
        if 'scenarios' not in sys.modules:
            scenarios_init = project_root / "scenarios" / "__init__.py"
            if scenarios_init.exists():
                spec = importlib.util.spec_from_file_location("scenarios", scenarios_init)
                scenarios_pkg = importlib.util.module_from_spec(spec)
                sys.modules["scenarios"] = scenarios_pkg
                spec.loader.exec_module(scenarios_pkg)

        # 2. Create 'scenarios.{scenario_name}' package if not exists
        scenario_pkg_name = f"scenarios.{scenario_name}"
        if scenario_pkg_name not in sys.modules:
            scenario_init = project_root / "scenarios" / scenario_name / "__init__.py"
            if scenario_init.exists():
                spec = importlib.util.spec_from_file_location(scenario_pkg_name, scenario_init)
                scenario_pkg = importlib.util.module_from_spec(spec)
                sys.modules[scenario_pkg_name] = scenario_pkg
                spec.loader.exec_module(scenario_pkg)

        # 3. Create 'scenarios.{scenario_name}.attacks' package if not exists
        attacks_pkg_name = f"scenarios.{scenario_name}.attacks"
        if attacks_pkg_name not in sys.modules:
            attacks_init = project_root / "scenarios" / scenario_name / "attacks" / "__init__.py"
            if attacks_init.exists():
                spec = importlib.util.spec_from_file_location(attacks_pkg_name, attacks_init)
                attacks_pkg = importlib.util.module_from_spec(spec)
                sys.modules[attacks_pkg_name] = attacks_pkg
                spec.loader.exec_module(attacks_pkg)

        # 4. Now load the actual scenario_impl module
        scenario_path = project_root / "scenarios" / scenario_name / "scenario_impl.py"
        if not scenario_path.exists():
            raise FileNotFoundError(f"Scenario not found: {scenario_path}")

        spec = importlib.util.spec_from_file_location(
            f"scenarios.{scenario_name}.scenario_impl",
            scenario_path
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        return module

    def _format_results(self, result: GreenEvaluatorResult) -> str:
        """Format green evaluator results as text"""
        avg_asr_pct = result.metrics.avg_asr * 100
        avg_detection_pct = (1 - result.metrics.avg_asr) * 100

        lines = [
            "=" * 80,
            "GREEN EVALUATOR RESULTS",
            "=" * 80,
            "",
            f"Evaluator: {result.evaluator_name}",
            f"Defender: {result.defender_name}",
            "",
            "--- Overall Metrics ---",
            f"Scenarios Tested: {result.metrics.scenario_coverage}",
            f"Total Attacks: {result.metrics.total_attacks}",
            f"Successful Attacks: {result.metrics.total_successes}",
            f"Average ASR: {avg_asr_pct:.2f}%",
            f"Average Detection Rate: {avg_detection_pct:.2f}%",
            f"Overall Efficiency: {result.metrics.overall_efficiency:.2f}",
            f"Baseline Correct Rate: {result.metrics.baseline_correct_rate:.1%}",
            "",
            "--- Phase Scores ---",
            f"Phase 1 Score (Attacker): {result.phase1_score:.3f}",
            f"Phase 2 Score (Defender): {result.phase2_score:.3f}",
            "",
            f"Winner: {'DEFENDER' if avg_detection_pct > 50 else 'ATTACKER'}",
            "",
            f"Total Time: {result.total_time_elapsed:.2f}s",
            "",
            "=" * 80,
            ""
        ]

        # Add per-scenario breakdown
        for scenario in result.scenarios:
            scenario_asr_pct = scenario.metrics.asr * 100
            lines.extend([
                "",
                f"--- Scenario: {scenario.scenario_name} ---",
                f"ASR: {scenario_asr_pct:.2f}%",
                f"Attacks: {scenario.metrics.total_attempts}",
                f"Successes: {scenario.metrics.successful_manipulations}",
                f"Coverage: {scenario.metrics.coverage:.2f}",
                f"Efficiency: {scenario.metrics.efficiency:.2f}",
                f"Baseline: {'✓ PASSED' if scenario.metrics.baseline_correct else '✗ FAILED'}",
                f"Time: {scenario.time_elapsed:.2f}s",
                ""
            ])

            # Show attack details for this scenario
            for attack in scenario.attacks:
                status = "✓ SUCCESS" if attack.manipulation_detected else "✗ FAILED"
                lines.append(f"  {attack.strategy}: {status}")

        lines.append("=" * 80)
        return "\n".join(lines)


def create_app(host: str, port: int, evaluator_name: str, scenario_configs: list[dict[str, Any]]):
    """Create and configure the A2A application"""
    card = AgentCard(
        name=f"{evaluator_name} Evaluator",
        description=f"Red team evaluator running {len(scenario_configs)} scenario(s)",
        url=f"http://{host}:{port}/",
        version="1.0.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=False),
        skills=[]
    )

    green_agent = GenericGreenEvaluator(evaluator_name, scenario_configs)
    executor = GreenExecutor(green_agent)

    task_store = InMemoryTaskStore()
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store
    )

    app = A2AStarletteApplication(
        agent_card=card,
        http_handler=request_handler
    )

    return app


async def main():
    parser = argparse.ArgumentParser(description="Generic Green Evaluator")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--port", type=int, default=9009, help="Port to bind to")
    parser.add_argument("--evaluator-name", required=True, help="Name of this evaluator")
    parser.add_argument("--scenarios", required=True, help="JSON string of scenario configs")
    args = parser.parse_args()

    import json
    scenario_configs = json.loads(args.scenarios)

    app = create_app(args.host, args.port, args.evaluator_name, scenario_configs)

    logger.info(f"Starting {args.evaluator_name} on {args.host}:{args.port}")
    logger.info(f"Configured scenarios: {[s['name'] for s in scenario_configs]}")

    uvicorn_config = uvicorn.Config(app.build(), host=args.host, port=args.port)
    uvicorn_server = uvicorn.Server(uvicorn_config)
    await uvicorn_server.serve()


if __name__ == "__main__":
    asyncio.run(main())
