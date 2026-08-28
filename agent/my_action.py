import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context

_battle_route_target = ""
_recognition_timeout_ms: int | None = None
_recognition_node_names: tuple[str, ...] | None = None


def _format_sanity_full_time(ocr_text: str, now: datetime | None = None) -> str | None:
    """Return the full-stamina estimate parsed from an OCR value such as ``42/100``."""
    match = re.search(r"(\d{1,3})\s*/\s*(\d{1,3})", ocr_text)
    if not match:
        return None
    current, maximum = (int(value) for value in match.groups())
    if maximum <= 0 or current < 0 or current > maximum:
        return None

    remaining_minutes = (maximum - current) * 6
    full_at = (now or datetime.now()).replace(second=0, microsecond=0) + timedelta(minutes=remaining_minutes)
    hours, minutes = divmod(remaining_minutes, 60)
    if remaining_minutes == 0:
        estimate = f"体力已回满（{full_at:%Y-%m-%d %H:%M}）"
    elif hours:
        estimate = f"预计 {hours} 小时 {minutes} 分钟 后回满（{full_at:%Y-%m-%d %H:%M}）"
    else:
        estimate = f"预计 {minutes} 分钟 后回满（{full_at:%Y-%m-%d %H:%M}）"
    return (
        f"**当前体力：{current}/{maximum}**\n\n"
        f"{estimate}"
    )


def _strip_json_comments(source: str) -> str:
    """Remove JSONC comments without interpreting comment markers inside strings."""
    result: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(source):
        char = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
        elif char == '"':
            in_string = True
            result.append(char)
            index += 1
        elif char == "/" and following == "/":
            index += 2
            while index < len(source) and source[index] not in "\r\n":
                index += 1
        elif char == "/" and following == "*":
            index += 2
            while index + 1 < len(source) and source[index:index + 2] != "*/":
                index += 1
            index += 2
        else:
            result.append(char)
            index += 1
    return "".join(result)


def _recognition_nodes() -> tuple[str, ...]:
    global _recognition_node_names
    if _recognition_node_names is not None:
        return _recognition_node_names

    project_root = Path(__file__).resolve().parents[1]
    resource_roots = (
        project_root / "resource",
        project_root / "assets" / "resource",
        Path.cwd() / "resource",
    )
    node_names: set[str] = set()
    parsed_files: set[Path] = set()
    for resource_root in resource_roots:
        for pipeline_root in (resource_root / "pipeline", resource_root / "EX" / "pipeline"):
            if not pipeline_root.is_dir():
                continue
            for pipeline_file in pipeline_root.rglob("*.json"):
                resolved = pipeline_file.resolve()
                if resolved in parsed_files:
                    continue
                parsed_files.add(resolved)
                try:
                    data = json.loads(_strip_json_comments(pipeline_file.read_text(encoding="utf-8-sig")))
                except (OSError, json.JSONDecodeError) as error:
                    print(f"RecognitionTimeout: skipped {pipeline_file.name}: {error}")
                    continue
                for name, node in data.items():
                    if isinstance(node, dict) and "recognition" in node:
                        node_names.add(name)

    _recognition_node_names = tuple(sorted(node_names))
    return _recognition_node_names


@AgentServer.custom_action("ConfigureRecognitionTimeout")
class ConfigureRecognitionTimeout(CustomAction):
    """Convert the user-selected timeout in seconds for later recognition overrides."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        global _recognition_timeout_ms
        try:
            raw_timeout = json.loads(argv.custom_action_param).get("timeout", "")
        except (json.JSONDecodeError, AttributeError):
            raw_timeout = ""
        value = str(raw_timeout).strip()
        if not value:
            _recognition_timeout_ms = None
            print("RecognitionTimeout: using each node's default timeout")
            return True
        try:
            timeout_seconds = int(value)
        except ValueError:
            print(f"RecognitionTimeout: ignored invalid timeout {value!r}")
            return True
        if timeout_seconds <= 0:
            print(f"RecognitionTimeout: ignored non-positive timeout {timeout_seconds} s")
            return True
        timeout_ms = timeout_seconds * 1000
        _recognition_timeout_ms = timeout_ms
        print(
            "RecognitionTimeout: global timeout set to "
            f"{timeout_seconds} s ({timeout_ms} ms)"
        )
        return True


@AgentServer.custom_action("ApplyRecognitionTimeout")
class ApplyRecognitionTimeout(CustomAction):
    """Apply the configured timeout to recognition nodes in the current task context."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        if _recognition_timeout_ms is None:
            print("RecognitionTimeout: no global override; keeping node defaults")
            return True
        override: dict[str, dict[str, int]] = {}
        for node_name in _recognition_nodes():
            node = context.get_node_data(node_name)
            if isinstance(node, dict) and "recognition" in node:
                override[node_name] = {"timeout": _recognition_timeout_ms}
        if not override:
            print("RecognitionTimeout: no recognition nodes found in this task context")
            return True
        succeeded = context.override_pipeline(override)
        print(
            f"RecognitionTimeout: applied {_recognition_timeout_ms} ms to "
            f"{len(override)} recognition nodes ({'success' if succeeded else 'failed'})"
        )
        return succeeded


@AgentServer.custom_action("ReportSanityFullTime")
class ReportSanityFullTime(CustomAction):
    """Calculate stamina and optionally pass it to a following Maa UI display node."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        result = getattr(argv.reco_detail, "best_result", None)
        ocr_text = getattr(result, "text", "")
        message = _format_sanity_full_time(str(ocr_text))
        if message is None:
            print(f"SanityCurrent: unable to parse OCR result {ocr_text!r}")
            return True

        try:
            params = json.loads(argv.custom_action_param or "{}")
        except json.JSONDecodeError:
            params = {}
        display_node = params.get("display_node") if isinstance(params, dict) else None
        if display_node:
            if not context.override_pipeline(
                {display_node: {"focus": {"Node.Action.Succeeded": message}}}
            ):
                print(f"SanityCurrent: unable to update UI node {display_node!r}")
                return False

        # Agent stdout remains available in the runtime log for diagnostics.
        print(f"体力回满预估：{message.replace(chr(10), ' ')}")
        return True


def _click_recognition_box(context: Context, argv: CustomAction.RunArg) -> bool:
    """Click the center of the recognition box, when one is available."""
    box = getattr(argv, "box", None)
    if not box:
        return True
    x = getattr(box, "x", 0)
    y = getattr(box, "y", 0)
    w = getattr(box, "w", 0)
    h = getattr(box, "h", 0)
    result = context.tasker.controller.post_click(int(x + w / 2), int(y + h / 2)).wait()
    return not hasattr(result, "succeeded") or result.succeeded

@AgentServer.custom_action("DFCleanBattleBridge")
class DFCleanBattleBridge(CustomAction):
    """Click a DF-C stage, run the shared BattleModule, then continue navigation."""

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        if not _click_recognition_box(context, argv):
            return False

        # Stage groups return to their chapter map after the shared battle:
        # 1-6 -> DF_C_1, 7-15 -> DF_C_2, 16-22 -> DF_C_3,
        # 23-31 -> DF_C_4, 32 -> return home, then finish.
        stage_number = int(argv.node_name.rsplit("_", 1)[-1])
        if stage_number <= 6:
            target = "DF_C_1"
        elif stage_number <= 15:
            target = "DF_C_2"
        elif stage_number <= 22:
            target = "DF_C_3"
        elif stage_number <= 31:
            target = "DF_C_4"
        elif stage_number <= 38:
            target = "DF_C_5"
        else:
            target = "DF_CleanBackHome"
        print(f"DFCleanBattleBridge: {argv.node_name} -> {target or '<end>'}")
        global _battle_route_target
        _battle_route_target = target
        print(f"DFCleanBattleBridge: battlemoduleend -> {target}")
        return context.override_next(argv.node_name, ["EstimateBegin"])


@AgentServer.custom_action("HT3CleanBattleBridge")
class HT3CleanBattleBridge(CustomAction):
    """Click an HT3-C stage, run the shared BattleModule, then continue navigation."""

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        if not _click_recognition_box(context, argv):
            return False

        stage_number = int(argv.node_name.rsplit("_", 1)[-1])
        if stage_number <= 10:
            target = "HT3_C_1"
        elif stage_number <= 15:
            target = "HT3_C_2"
        elif stage_number <= 20:
            target = "HT3_C_3"
        elif stage_number <= 26:
            target = "HT3_C_4"
        else:
            target = "HT3_CleanBackHome"

        print(f"HT3CleanBattleBridge: {argv.node_name} -> {target}")
        global _battle_route_target
        _battle_route_target = target
        return context.override_next(argv.node_name, ["EstimateBegin"])


@AgentServer.custom_action("DFCleanBattleEndRoute")
class DFCleanBattleEndRoute(CustomAction):
    """Route the shared BattleModule terminator back to the DF-C map."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        target = _battle_route_target
        if target == "STClean_0_swipe_down":
            next_nodes = ["STC0_new_character", target]
        elif target == "STClean_0_swipe_up":
            next_nodes = ["STC0_new_character_1", target]
        else:
            next_nodes = [target]
        print(f"DFCleanBattleEndRoute: {target} -> {next_nodes}")
        return context.override_next(argv.node_name, next_nodes)


@AgentServer.custom_action("STClean0BattleBridge")
class STClean0BattleBridge(CustomAction):
    """Run the shared BattleModule and return to the next main-story chapter."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        if not _click_recognition_box(context, argv):
            return False

        stage_number = int(argv.node_name.rsplit("_", 1)[-1])
        if stage_number <= 14:
            target = "STClean_0_swipe_up"
        elif stage_number <= 37:
            target = "STClean_0_swipe_down"
        else:
            target = "STClean_0BackHome"

        global _battle_route_target
        _battle_route_target = target
        print(f"STClean0BattleBridge: {argv.node_name} -> {target}")
        return context.override_next(argv.node_name, ["EstimateBegin"])


@AgentServer.custom_action("STClean1BattleBridge")
class STClean1BattleBridge(CustomAction):
    """Run the shared BattleModule and return to the Chapter 1 map."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        if not _click_recognition_box(context, argv):
            return False

        stage_number = int(argv.node_name.rsplit("_", 1)[-1])
        target = "STClean_1_swipe_up" if stage_number <= 34 else "STClean_1BackHome"

        global _battle_route_target
        _battle_route_target = target
        print(f"STClean1BattleBridge: {argv.node_name} -> {target}")
        return context.override_next(argv.node_name, ["EstimateBegin"])


@AgentServer.custom_action("STClean2BattleBridge")
class STClean2BattleBridge(CustomAction):
    """Run the shared BattleModule and return to the Chapter 2 map."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        if not _click_recognition_box(context, argv):
            return False

        stage_number = int(argv.node_name.rsplit("_", 1)[-1])
        target = "STClean_2_swipe_down" if stage_number <= 26 else "STClean_2BackHome"

        global _battle_route_target
        _battle_route_target = target
        print(f"STClean2BattleBridge: {argv.node_name} -> {target}")
        return context.override_next(argv.node_name, ["EstimateBegin"])


@AgentServer.custom_action("STClean3BattleBridge")
class STClean3BattleBridge(CustomAction):
    """Run the shared BattleModule and route back to the appropriate Chapter 3 map."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        if not _click_recognition_box(context, argv):
            return False

        stage_number = int(argv.node_name.rsplit("_", 1)[-1])
        if stage_number <= 6:
            target = "STClean_3_swipe_down_1"
        elif stage_number <= 18:
            target = "STClean_3_swipe_down_2_1"
        elif stage_number <= 23:
            target = "STClean_3_swipe_down_3_1"
        elif stage_number <= 32:
            target = "STClean_3_swipe_down_4_1"
        else:
            target = "STClean_3BackHome"

        global _battle_route_target
        _battle_route_target = target
        print(f"STClean3BattleBridge: {argv.node_name} -> {target}")
        return context.override_next(argv.node_name, ["EstimateBegin"])
