from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context

_df_clean_target = ""

@AgentServer.custom_action("DFCleanBattleBridge")
class DFCleanBattleBridge(CustomAction):
    """Click a DF-C stage, run the shared BattleModule, then continue navigation."""

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        # Preserve the original Click action of the stage node.
        box = getattr(argv, "box", None)
        if box:
            # Maa returns a maa.define.Rect, not a sequence.
            x = getattr(box, "x", 0)
            y = getattr(box, "y", 0)
            w = getattr(box, "w", 0)
            h = getattr(box, "h", 0)
            click_job = context.tasker.controller.post_click(
                int(x + w / 2), int(y + h / 2)
            )
            click_result = click_job.wait()
            if hasattr(click_result, "succeeded") and not click_result.succeeded:
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
        global _df_clean_target
        _df_clean_target = target
        print(f"DFCleanBattleBridge: battlemoduleend -> {target}")
        return context.override_next(argv.node_name, ["EstimateBegin"])


@AgentServer.custom_action("DFCleanBattleEndRoute")
class DFCleanBattleEndRoute(CustomAction):
    """Route the shared BattleModule terminator back to the DF-C map."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        target = _df_clean_target
        print(f"DFCleanBattleEndRoute: {target}")
        return context.override_next(argv.node_name, [target])
