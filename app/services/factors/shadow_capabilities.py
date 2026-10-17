"""The historical Shadow experiment has no connected live observation producer.

Keep stored evidence readable, but never turn manually supplied metrics or dates
into production admission evidence. Connecting the collector requires a separate
implementation and acceptance; a settings switch cannot enable this capability.
"""

SHADOW_BLOCK_CODE = "shadow_collection_not_connected"
SHADOW_BLOCK_REASON = "Shadow 日频观测采集尚未接通，历史手工观测不能作为生产上线证据。当前仅支持查看历史，暂不开放写入和上线审批。"


def shadow_capabilities() -> dict:
    blocked = {"supported": False, "reason": SHADOW_BLOCK_REASON,
               "error_code": SHADOW_BLOCK_CODE}
    return {"history": {"supported": True},
            "observation_collection": dict(blocked),
            "activation": dict(blocked), "mode": "historical_read_only"}
