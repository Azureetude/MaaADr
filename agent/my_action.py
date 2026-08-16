from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context

_battle_route_target = ""


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
        # 23 -> return home, then finish.
        stage_number = int(argv.node_name.rsplit("_", 1)[-1])
        if stage_number <= 6:
            target = "DF_C_1"
        elif stage_number <= 15:
            target = "DF_C_2"
        elif stage_number <= 22:
            target = "DF_C_3"
        else:
            target = "DF_CleanBackHome"
        print(f"DFCleanBattleBridge: {argv.node_name} -> {target or '<end>'}")
        global _battle_route_target
        _battle_route_target = target
        print(f"DFCleanBattleBridge: battlemoduleend -> {target}")
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
