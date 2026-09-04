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

_SWITCHACCOUNT_FILE = Path(__file__).resolve().parent.parent / "config" / "switchaccount.json"


def _load_switchaccount(profile: str = "default") -> dict:
    try:
        file_path = _SWITCHACCOUNT_FILE
        if not file_path.exists():
            candidates = [Path.cwd() / "config" / "switchaccount.json", Path.cwd().parent / "config" / "switchaccount.json"]
            file_path = next((p for p in candidates if p.exists()), file_path)
        data = json.loads(file_path.read_text(encoding="utf-8"))
        # New format stores profiles under `profiles`; migrate old flat data.
        if isinstance(data, dict) and isinstance(data.get("profiles"), dict):
            return data["profiles"].get(profile, {})
        return data if profile == "default" and isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_switchaccount(data: dict, profile: str = "default") -> None:
    _SWITCHACCOUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        root = json.loads(_SWITCHACCOUNT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        root = {}
    profiles = root.get("profiles") if isinstance(root, dict) else None
    if not isinstance(profiles, dict):
        profiles = {"default": root} if isinstance(root, dict) and root else {}
    profiles[profile] = data
    _SWITCHACCOUNT_FILE.write_text(json.dumps({"profiles": profiles}, ensure_ascii=False, indent=2), encoding="utf-8")


@AgentServer.custom_action("SaveSwitchAccount")
class SaveSwitchAccount(CustomAction):
    """Persist account, password and remark supplied by the task UI."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            params = json.loads(argv.custom_action_param or "{}")
        except (json.JSONDecodeError, TypeError):
            params = {}
        profile = str(params.get("profile", "default")).strip() or "default"
        old = _load_switchaccount(profile)
        data = dict(old)
        if "account" in params:
            data["account"] = str(params.get("account", "")).strip() or old.get("account", "")
        if "password" in params:
            data["password"] = str(params.get("password", "")) or old.get("password", "")
        if "remark" in params:
            data["remark"] = str(params.get("remark", "")).strip()
        _save_switchaccount(data, profile)
        print("SwitchAccount: account information saved")
        return True


def _save_switchaccount_field(field: str, value: object, profile: str = "default") -> None:
    data = _load_switchaccount(profile)
    if field == "account":
        data[field] = str(value).strip() or data.get(field, "")
    elif field == "password":
        data[field] = str(value) or data.get(field, "")
    else:
        data[field] = str(value).strip()
    _save_switchaccount(data, profile)


class _SaveSwitchAccountField(CustomAction):
    field = ""
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            params = json.loads(argv.custom_action_param or "{}")
        except (json.JSONDecodeError, TypeError):
            params = {}
        profile = str(params.get("profile", "default")).strip() or "default"
        _save_switchaccount_field(self.field, params.get(self.field, ""), profile)
        return True


@AgentServer.custom_action("SaveSwitchAccountAccount")
class SaveSwitchAccountAccount(_SaveSwitchAccountField):
    field = "account"


@AgentServer.custom_action("SaveSwitchAccountPassword")
class SaveSwitchAccountPassword(_SaveSwitchAccountField):
    field = "password"


@AgentServer.custom_action("SaveSwitchAccountRemark")
class SaveSwitchAccountRemark(_SaveSwitchAccountField):
    field = "remark"


def _input_saved_switchaccount(context: Context, field: str, profile: str = "default") -> bool:
    value = _load_switchaccount(profile).get(field, "")
    if not value:
        print(f"SwitchAccount: missing {field}")
        return False
    try:
        job = context.tasker.controller.post_input_text(str(value))
        if job is not None and hasattr(job, "wait"):
            job.wait()
        return True
    except Exception as exc:
        # Text injection failures must fail the pipeline immediately.  Keep
        # credentials out of the diagnostic message.
        print(f"SwitchAccount: text input for {field} failed ({type(exc).__name__})")
        return False


@AgentServer.custom_action("InputSwitchAccountName")
class InputSwitchAccountName(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        return _input_saved_switchaccount(context, "account")


@AgentServer.custom_action("InputSwitchAccountPassword")
class InputSwitchAccountPassword(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        return _input_saved_switchaccount(context, "password")


@AgentServer.custom_action("ReportSwitchAccountLogin")
class ReportSwitchAccountLogin(CustomAction):
    """Show the account used after the login click succeeds."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        account = str(_load_switchaccount().get("account", "")).strip()
        message = f"当前登录账号为{account or '未知'}"
        try:
            context.override_pipeline({
                argv.node_name: {
                    "focus": {"Node.Action.Succeeded": message}
                }
            })
        except Exception:
            pass
        return True


def _parse_positive_int(text: object) -> int | None:
    """Parse an OCR number, tolerating grouping separators and surrounding text."""
    match = re.search(r"\d[\d,，\s]*", str(text))
    if not match:
        return None
    try:
        return int(re.sub(r"[^0-9]", "", match.group(0)))
    except ValueError:
        return None


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


@AgentServer.custom_action("CheckCopperEtherForExchange")
class CheckCopperEtherForExchange(CustomAction):
    """Verify the OCR'd copper ether can pay for all selected daily exchanges."""

    _selection_nodes = (
        "ExchangeSelectEnergyDrink",
        "ExchangeSelectPurpleGift",
        "ExchangeSelectBattleRecord",
        "ExchangeSelectDragonCoin",
    )

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        result = getattr(argv.reco_detail, "best_result", None)
        balance = _parse_positive_int(getattr(result, "text", ""))
        if balance is None:
            print("Exchange: unable to parse current copper ether")
            return False
        selected = 0
        for node_name in self._selection_nodes:
            node = context.get_node_data(node_name)
            if isinstance(node, dict) and node.get("enabled", False):
                selected += 1
        cost = selected * 5
        if balance < cost:
            message = "目前兑换所需铜以太不足"
            context.override_pipeline(
                {
                    "ether_3": {
                        "focus": {"Node.Action.Succeeded": message},
                        "action": {"type": "DoNothing"},
                        "next": ["endexchange"],
                    }
                }
            )
            context.override_next(argv.node_name, ["ether_3"])
            print(f"Exchange: {message} (current={balance}, required={cost})")
            return True
        message = f"本次兑换共消耗 {cost} 铜以太，剩余 {balance - cost} 铜以太"
        context.override_pipeline(
            {
                "ether_3": {
                    "focus": {"Node.Action.Succeeded": message},
                    "next": ["if_精力饮"],
                }
            }
        )
        context.override_next(argv.node_name, ["ether_3"])
        print(f"Exchange: {message}")
        return True


@AgentServer.custom_action("OpenCopperEtherAndCheck")
class OpenCopperEtherAndCheck(CustomAction):
    """Open the copper-ether shop then force the balance check before purchases."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        # Keep the original copperymana click target, but do not permit legacy
        # task-option overrides to bypass ether_1 -> ether_2 afterwards.
        result = context.tasker.controller.post_click(616, 132).wait()
        if hasattr(result, "succeeded") and not result.succeeded:
            return False
        return context.override_next(argv.node_name, ["ether_1"])


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
        elif stage_number <= 41:
            target = "DF_C_5"
        elif stage_number <= 44:
            target = "DF_C_6"
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


@AgentServer.custom_action("STClean4BattleBridge")
class STClean4BattleBridge(CustomAction):
    """Run the shared BattleModule for chapter 4 and return to its map."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        if not _click_recognition_box(context, argv):
            return False
        # Chapter 4 stages 1-29 are on the same map page.  Stage 30 is the
        # final stage and proceeds through the chapter back-home/finish flow.
        match = re.search(r"_(\d+)$", argv.node_name)
        stage_number = int(match.group(1)) if match else 0
        target = "STClean_4_swipe_down_1" if stage_number <= 29 else "STClean_4BackHome"
        global _battle_route_target
        _battle_route_target = target
        print(f"STClean4BattleBridge: {argv.node_name} -> {target}")
        return context.override_next(argv.node_name, ["EstimateBegin"])


@AgentServer.custom_action("STClean5BattleBridge")
class STClean5BattleBridge(CustomAction):
    """Run the shared BattleModule for chapter 5 and route back to its map."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        if not _click_recognition_box(context, argv):
            return False
        match = re.search(r"_(\d+)$", argv.node_name)
        stage_number = int(match.group(1)) if match else 0
        target = "STClean_5_swipe_up_1" if stage_number <= 36 else "STClean_5BackHome"
        global _battle_route_target
        _battle_route_target = target
        print(f"STClean5BattleBridge: {argv.node_name} -> {target}")
        return context.override_next(argv.node_name, ["EstimateBegin"])


@AgentServer.custom_action("STClean6BattleBridge")
class STClean6BattleBridge(CustomAction):
    """Run the shared BattleModule and route chapter 6 stages to their map pages."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        if not _click_recognition_box(context, argv):
            return False
        match = re.search(r"_(\d+)$", argv.node_name)
        stage_number = int(match.group(1)) if match else 0
        if stage_number <= 15:
            target = "STClean_6_swipe_down_1"
        elif stage_number <= 34:
            target = "STClean_6_swipe_down_2"
        elif stage_number <= 42:
            target = "STClean_6_swipe_down_3"
        else:
            target = "STClean_6BackHome"
        global _battle_route_target
        _battle_route_target = target
        print(f"STClean6BattleBridge: {argv.node_name} -> {target}")
        return context.override_next(argv.node_name, ["EstimateBegin"])


@AgentServer.custom_action("STClean7BattleBridge")
class STClean7BattleBridge(CustomAction):
    """Run the shared BattleModule and return chapter 7 stages to its map."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        if not _click_recognition_box(context, argv):
            return False
        match = re.search(r"_(\d+)$", argv.node_name)
        stage_number = int(match.group(1)) if match else 0
        target = "STClean_7_swipe_up" if stage_number <= 43 else "STClean_7BackHome"
        global _battle_route_target
        _battle_route_target = target
        print(f"STClean7BattleBridge: {argv.node_name} -> {target}")
        return context.override_next(argv.node_name, ["EstimateBegin"])
