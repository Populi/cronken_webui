import base64
import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import AsyncIterable, Dict, List, Union

import humanize
from aiohttp import web
from aiohttp_jinja2 import get_env as jinja_env
from aiohttp_jinja2 import setup as jinja_setup
from aiohttp_jinja2 import template as jinja_template
from coredis import Redis, RedisCluster
from jinja2 import FileSystemLoader

REQUIRED_CONFIGS = ["redis_info"]
ALL_CONFIGS = {
    "namespace": "{cronken}",
    "log_level": "DEBUG"
}
ALL_CONFIGS.update({k: None for k in REQUIRED_CONFIGS})


def get_config(prefix: str, all_configs: Dict) -> Dict:
    final_config = {}
    for key, default_value in all_configs.items():
        env_value = os.environ.get(f"{prefix}_{key.upper()}", None)

        # Cast env_value to the correct type, if it exists
        if env_value is not None:
            if isinstance(default_value, bool):
                # bools in Python are instances of both bool and int, so this check needs to be before the>
                env_value = env_value.lower() == "true"
            elif isinstance(default_value, int):
                env_value = int(env_value)

        if env_value is None and default_value is None:
            raise Exception(f"Required config entry '{key}' or env var '{prefix}_{key.upper()}' not set")

        final_config[key] = env_value if env_value is not None else default_value

    # Special processing for the redis node(s) to convert from "foo:1234,bar:5678" to structured data
    if "redis_info" in final_config:
        final_config["redis_info"] = [
            {"host": node.split(":")[0], "port": int(node.split(":")[1])}
            for node in final_config["redis_info"].split(",")
        ]

    return final_config


class CronkenInfo:
    def __init__(self, redis_info: Union[List[Dict], Dict], namespace: str = "{cronken}", log_level: str = "DEBUG"):
        if type(redis_info) is dict:
            # If we're passed a single host/port dict, assume it's a non-clustered Redis server
            self.rclient = Redis(**redis_info)
        else:
            # Assume it's a list of host/port dicts and init it as a RedisCluster
            self.rclient = RedisCluster(startup_nodes=redis_info)
        self.namespace = namespace
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(log_level.upper())
        stdout_handler = logging.StreamHandler()
        stdout_handler.setLevel(log_level.upper())
        formatter = logging.Formatter(fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        stdout_handler.setFormatter(formatter)
        self.logger.addHandler(stdout_handler)

    async def get_output(self, run_id: str, max_chunks: int = 1000) -> List[str]:
        if max_chunks == 0:
            return []

        lines = []
        for chunk in await self.rclient.lrange(f"{self.namespace}:rundata:output:{run_id}", -max_chunks, -1):
            lines.extend(chunk.decode("utf-8").rstrip().split("\n"))
        return lines

    async def num_output_chunks(self, run_id: str) -> int:
        return self.rclient.llen(f"{self.namespace}:output:{run_id}")

    async def send_command(self, action: str, args):
        data = json.dumps({"action": action, "args": args}).encode("utf-8")
        await self.rclient.publish(channel=f"{self.namespace}:__events__", message=data)

    @staticmethod
    def pretty_schedule(job_def: dict) -> str:
        # If the cronstring exists, we can just use it verbatim
        if job_def.get("cron_args", {}).get("cronstring", ""):
            return job_def["cron_args"]["cronstring"]
        # Otherwise, output each key/value pair separated by a linebreak
        return "<br />".join([f"{k}:{v}" for k, v in job_def.get("cron_args", {}).items() if v])

    async def get_job(self, job_name: str) -> Dict[str, dict]:
        raw_def = await self.rclient.hget(f"{self.namespace}:jobs", job_name)
        if raw_def:
            try:
                job_def = json.loads(raw_def)
            except json.JSONDecodeError:
                self.logger.warning(f"Couldn't parse job {job_name} definition {raw_def}")
                return {}
            return job_def
        return {}

    async def get_jobs(self) -> Dict[str, Dict]:
        jobs = {}
        raw_jobs = await self.rclient.hgetall(f"{self.namespace}:jobs")
        for raw_name, raw_def in raw_jobs.items():
            try:
                job_name = raw_name.decode("utf-8")
                job_def = json.loads(raw_def)
            except (json.JSONDecodeError, UnicodeError):
                self.logger.warning(f"Couldn't parse job {raw_name} definition {raw_def}")
                continue
            self.logger.debug(f"Found job {job_name} with definition {job_def}")
            jobs[job_name] = job_def
        return jobs

    async def delete_job(self, job_name: str):
        await self.rclient.hdel(f"{self.namespace}:jobs", [job_name])

    async def set_job(self, job_name: str, job_def: dict):
        encoded_job = json.dumps(job_def)
        await self.rclient.hset(f"{self.namespace}:jobs", {job_name: encoded_job})

    async def recent_runs(self, key: str, limit: int = 10, offset: int = 0) -> AsyncIterable[dict]:
        if limit > 0:
            end_offset = (offset + 1) * -1
            start_offset = end_offset - limit + 1
            run_ids = await self.rclient.zrange(f"{self.namespace}:{key}", start_offset, end_offset)
        else:
            run_ids = await self.rclient.zrange(f"{self.namespace}:{key}", 0, -1)

        raw_runs = {}
        if run_ids:
            bytes_runs = await self.rclient.hmget(f"{self.namespace}:rundata", run_ids)
            raw_runs = {run_ids[i].decode("utf-8"): bytes_runs[i].decode("utf-8") for i in range(len(run_ids))}

        for run_id in reversed(run_ids):
            try:
                run = json.loads(raw_runs[run_id.decode("utf-8")])
            except json.JSONDecodeError:
                self.logger.warning(f"Invalid json data for run_id {run_id}")
                continue
            run["run_id"] = run_id.decode("utf-8")
            run["start_time"] = humanize.naturaltime(datetime.fromtimestamp(float(run["start_time"])))
            if run.get("duration", None):
                run["duration"] = humanize.naturaldelta(run["duration"])
            yield run


routes = web.RouteTableDef()
routes.static("/static", Path(__file__).parent.absolute() / "static_files")


@routes.get("/")
async def index(_):
    raise web.HTTPFound("/dashboard")


@routes.get("/dashboard")
@jinja_template("dashboard.html.j2")
async def dashboard(request):
    cronken: CronkenInfo = request.app["cronken"]
    active_runs = [x async for x in cronken.recent_runs("rundata:active", 0)]
    completed_runs = [x async for x in cronken.recent_runs("results:success", 20)]
    failed_runs = [x async for x in cronken.recent_runs("results:fail", 20)]
    return {"active_runs": active_runs, "completed_runs": completed_runs, "failed_runs": failed_runs}


@routes.get("/completed")
@jinja_template("completed.html.j2")
async def completed(request):
    cronken: CronkenInfo = request.app["cronken"]
    runs = [x async for x in cronken.recent_runs("results:success", 100)]
    return {"runs": runs}


@routes.get("/failed")
@jinja_template("failed.html.j2")
async def completed(request):
    cronken: CronkenInfo = request.app["cronken"]
    runs = [x async for x in cronken.recent_runs("results:fail", 100)]
    return {"runs": runs}


@routes.get("/setup")
@jinja_template("setup.html.j2")
async def completed(request):
    cronken: CronkenInfo = request.app["cronken"]
    jobs = await cronken.get_jobs()
    for job_def in jobs.values():
        job_def["pretty_schedule"] = cronken.pretty_schedule(job_def)
    return {"jobs": jobs}


@routes.get("/jobs/{job_name}/completed")
@jinja_template("view_job_runs.html.j2")
async def runs_completed(request: web.Request):
    cronken: CronkenInfo = request.app["cronken"]
    job_name = request.match_info["job_name"]
    try:
        offset = int(request.rel_url.query.get("offset", 0))
    except ValueError:
        offset = 0

    try:
        limit = int(request.rel_url.query.get("limit", 100))
    except ValueError:
        limit = 100

    run_data = [x async for x in cronken.recent_runs(f"results:{job_name}:success", limit=limit, offset=offset)]
    next_url = f"{request.rel_url.path}?offset={offset + 100}&limit={limit}"

    return {
        "queue_friendly_name": "Completed",
        "job_name": job_name,
        "run_data": run_data,
        "next_url": next_url if len(run_data) == limit else ""
    }


@routes.get("/jobs/{job_name}/failed")
@jinja_template("view_job_runs.html.j2")
async def runs_failed(request: web.Request):
    cronken: CronkenInfo = request.app["cronken"]
    job_name = request.match_info["job_name"]
    try:
        offset = int(request.rel_url.query.get("offset", 0))
    except ValueError:
        offset = 0

    try:
        limit = int(request.rel_url.query.get("limit", 100))
    except ValueError:
        limit = 100

    run_data = [x async for x in cronken.recent_runs(f"results:{job_name}:fail", limit=limit, offset=offset)]
    next_url = f"{request.rel_url.path}?offset={offset + 100}&limit={limit}"

    return {
        "queue_friendly_name": "Completed",
        "job_name": job_name,
        "run_data": run_data,
        "next_url": next_url if len(run_data) == limit else ""
    }


@routes.get("/runs/{run_id}/output")
@jinja_template("_output.html.j2")
async def run_output(request: web.Request):
    cronken: CronkenInfo = request.app["cronken"]
    run_id: str = request.match_info["run_id"]
    output = await cronken.get_output(run_id)
    return {"title": "run_output", "output": output}


@routes.put("/runs/{run_id}/rerun")
async def run_rerun(request: web.Request):
    cronken: CronkenInfo = request.app["cronken"]
    run_id: str = request.match_info["run_id"]
    raw_run_data = await cronken.rclient.hget(f"{cronken.namespace}:rundata", run_id)
    run_data = json.loads(raw_run_data.decode("utf-8"))
    await cronken.send_command("trigger", run_data["job_name"])
    return web.Response(status=200, text=f"Triggered {run_data['job_name']}")


@routes.put("/runs/{run_id}/terminate")
async def run_terminate(request: web.Request):
    cronken: CronkenInfo = request.app["cronken"]
    run_id: str = request.match_info["run_id"]
    await cronken.send_command("terminate_run", run_id)
    return web.Response(status=200, text=f"Terminated {run_id}")


@routes.get("/runs/{run_id}/kill")
async def run_kill(request: web.Request):
    cronken: CronkenInfo = request.app["cronken"]
    run_id: str = request.match_info["run_id"]
    await cronken.send_command("kill_run", run_id)
    return web.Response(status=200, text=f"Killed {run_id}")


@routes.get("/jobs/create")
@routes.get("/jobs/update")
@jinja_template("_job_form.html.j2")
async def show_update(request: web.Request):
    cronken: CronkenInfo = request.app["cronken"]
    job_name: str = request.rel_url.query.get("job_name", "")
    job_def_raw = await cronken.rclient.hget(f"{cronken.namespace}:jobs", job_name) if job_name else b"{}"
    try:
        job_def = json.loads(job_def_raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeError):
        cronken.logger.warning(f"Couldn't parse job {job_name}: {job_def_raw}")
        job_def = {}
    return {"job_name": job_name, "job_def": job_def}


@routes.post("/jobs/update")
async def update_job(request: web.Request):
    raw_post = await request.post()
    cronken: CronkenInfo = request.app["cronken"]
    job_name = raw_post.get("job_name", "").strip()
    if not raw_post.get("job_name", "").strip():
        raise web.HTTPBadRequest(reason="Missing job name")
    if not raw_post.get("job_args|cmd", "").strip():
        raise web.HTTPBadRequest(reason="Missing command")
    old_job_name = raw_post.get("original_name", "")

    job_def = defaultdict(dict)
    for raw_key, value in raw_post.items():
        if value and "|" in raw_key:
            section, key = raw_key.split("|", maxsplit=2)
            job_def[section][key] = value
    # coerce types
    job_def["job_args"]["lock"] = bool(int(job_def["job_args"]["lock"]))
    job_def["job_state"]["paused"] = bool(int(job_def["job_state"]["paused"]))
    try:
        job_def["job_args"]["ttl"] = int(job_def["job_args"]["ttl"])
    except (ValueError, KeyError):
        job_def["job_args"]["ttl"] = 10

    old_job_def = await cronken.get_job(old_job_name)
    persistent_state_identical = (
        old_job_def
        and job_name == old_job_name
        and job_def.get("cron_args", {}) == old_job_def.get("cron_args", {})
        and job_def.get("job_args", {}) == old_job_def.get("job_args", {})
    )
    old_pause = old_job_def.get("job_state", {}).get("paused", False)
    new_pause = job_def.get("job_state", {}).get("paused", False)
    pause_identical = old_pause == new_pause

    if not persistent_state_identical or not pause_identical:
        await cronken.set_job(job_name, job_def)
        await cronken.send_command("validate", job_name)

    if old_job_name and old_job_name != job_name:
        await cronken.delete_job(old_job_name)

    if not persistent_state_identical:
        await cronken.send_command("reload", {})

    if not pause_identical:
        action = "pause" if job_def.get("job_state", {}).get("paused", False) else "resume"
        await cronken.send_command(action, job_name)

    return web.Response(status=200, text="OK")


@routes.post("/jobs/{job_name}/trigger")
async def trigger_job(request: web.Request):
    cronken: CronkenInfo = request.app["cronken"]
    job_name: str = request.match_info["job_name"]

    await cronken.send_command("trigger", job_name)

    return web.Response(status=200, text=f"Triggered {job_name}")


@routes.post("/jobs/{job_name}/pause")
async def pause_job(request: web.Request):
    cronken: CronkenInfo = request.app["cronken"]
    job_name: str = request.match_info["job_name"]

    await cronken.send_command("pause", job_name)

    job_def = await cronken.get_job(job_name)
    if job_def:
        job_def = defaultdict(dict, job_def)
        job_def["job_state"]["paused"] = True
        await cronken.set_job(job_name, job_def)

    return web.Response(status=200, text=f"Paused {job_name}")


@routes.post("/jobs/{job_name}/resume")
async def resume_job(request: web.Request):
    cronken: CronkenInfo = request.app["cronken"]
    job_name: str = request.match_info["job_name"]

    await cronken.send_command("resume", job_name)

    job_def = await cronken.get_job(job_name)
    if job_def:
        job_def = defaultdict(dict, job_def)
        job_def["job_state"]["paused"] = False
        await cronken.set_job(job_name, job_def)

    return web.Response(status=200, text=f"Resumed {job_name}")


@routes.post("/jobs/{job_name}/delete")
async def delete_job(request: web.Request):
    cronken: CronkenInfo = request.app["cronken"]
    job_name: str = request.match_info["job_name"]

    await cronken.delete_job(job_name)

    return web.Response(status=200, text=f"Deleted {job_name}")


def main():
    app = web.Application()
    jinja_setup(app, loader=FileSystemLoader(Path(__file__).parent.absolute() / "templates"), autoescape=True)
    jinja_env(app).filters["b64encode"] = lambda x: base64.b64encode(x.encode("utf-8")).decode("utf-8")

    app.add_routes(routes)

    cronken = CronkenInfo(**get_config("CRONKEN_WEBUI", ALL_CONFIGS))
    app["cronken"] = cronken
    web.run_app(app, host="0.0.0.0", port=8885)


if __name__ == "__main__":
    main()
