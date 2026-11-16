"""MaaFramework 原生日志格式；供日志检索与绘图共用，不依赖消费者。"""
import re

LINE_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3,6})\]"
    r"\[(?P<lvl>[A-Z]{3})\]"
    r"\[(?P<px>Px\d+)\]\[Tx\d+\]"
    r"\[(?P<src>[^\]]+)\]\[L\d+\]\[(?P<func>[^\]]*)\]\s?(?P<msg>.*)$"
)
NODE_DONE_RE = re.compile(
    r"PipelineTask node done \[result=(\{.*\})\] \[task_id_=(\d+)\]\s*$"
)
TASK_START_RE = re.compile(r"task start: \[cb_detail=(\{.*\})\]\s*$")
TASK_END_RE = re.compile(r"task end: \[cb_detail=(\{.*\})\] \[ret=(\w+)\]\s*$")
