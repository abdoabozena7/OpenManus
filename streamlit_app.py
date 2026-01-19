import asyncio
import html
import json
import re
import sys
import threading
import time
import traceback
from queue import Queue

import streamlit as st

from app.agent.mcp import MCPAgent
from app.config import config
from app.llm import LLM


st.set_page_config(page_title="OpenManus MCP UI", page_icon=":compass:", layout="wide")

st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600&display=swap');
:root {
  --panel: #111827;
  --panel-2: #0b1220;
  --accent: #ff6b35;
  --accent-2: #14b8a6;
  --muted: #94a3b8;
  --glow: rgba(255, 107, 53, 0.35);
}
.stApp {
  background: radial-gradient(1200px 600px at 80% -10%, #0f172a 0%, #0b1120 60%, #070b14 100%);
  color: #e2e8f0;
}
section[data-testid="stSidebar"] {
  background: linear-gradient(180deg, #0b1220 0%, #0a0f1a 100%);
  border-right: 1px solid rgba(148, 163, 184, 0.1);
}
.stButton > button {
  background: linear-gradient(135deg, var(--accent) 0%, #ff8a4c 100%);
  color: #0b1220;
  border: none;
  border-radius: 12px;
  padding: 0.55rem 1.5rem;
  font-weight: 600;
  box-shadow: 0 10px 24px rgba(255, 107, 53, 0.35);
}
.stButton > button:hover {
  background: linear-gradient(135deg, #ff8a4c 0%, var(--accent) 100%);
}
.stTextArea textarea, .stTextInput input {
  background: #0f172a;
  color: #e2e8f0;
  border: 1px solid rgba(148, 163, 184, 0.2);
  border-radius: 12px;
}
.bubble {
  background: rgba(15, 23, 42, 0.65);
  border: 1px solid rgba(148, 163, 184, 0.2);
  border-radius: 16px;
  padding: 0.85rem 1rem;
}
.steps {
  display: grid;
  gap: 0.6rem;
}
.step {
  background: linear-gradient(160deg, rgba(20, 184, 166, 0.12), rgba(255, 107, 53, 0.1));
  border: 1px solid rgba(148, 163, 184, 0.2);
  border-radius: 14px;
  padding: 0.75rem 0.9rem;
  animation: fadeUp 0.35s ease-out both;
  box-shadow: 0 8px 20px rgba(15, 23, 42, 0.35);
}
.step strong {
  color: var(--accent-2);
}
.summary {
  background: linear-gradient(160deg, rgba(255, 107, 53, 0.18), rgba(20, 184, 166, 0.12));
  border: 1px solid rgba(148, 163, 184, 0.2);
  border-radius: 16px;
  padding: 1rem;
  box-shadow: 0 12px 28px rgba(15, 23, 42, 0.4);
}
.plan {
  display: grid;
  gap: 0.6rem;
}
.plan-step {
  background: rgba(15, 23, 42, 0.7);
  border: 1px solid rgba(148, 163, 184, 0.2);
  border-radius: 14px;
  padding: 0.75rem 0.9rem;
}
.plan-step.active {
  border-color: var(--accent);
  box-shadow: 0 8px 20px var(--glow);
}
.plan-step .status {
  color: var(--accent-2);
  font-weight: 600;
  margin-right: 0.5rem;
}
.plan-meta {
  color: var(--muted);
  font-size: 0.9rem;
}
@keyframes fadeUp {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}
</style>
""",
    unsafe_allow_html=True,
)

st.title("OpenManus MCP")
st.caption("Run the MCP agent with a single prompt.")

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

if "last_prompt" not in st.session_state:
    st.session_state.last_prompt = ""
if "last_error" not in st.session_state:
    st.session_state.last_error = ""
if "live_story" not in st.session_state:
    st.session_state.live_story = []
if "live_findings" not in st.session_state:
    st.session_state.live_findings = []
if "summary_text" not in st.session_state:
    st.session_state.summary_text = ""
if "blocked_by_captcha" not in st.session_state:
    st.session_state.blocked_by_captcha = False
if "story_keys" not in st.session_state:
    st.session_state.story_keys = set()
if "plan_data" not in st.session_state:
    st.session_state.plan_data = None

with st.sidebar:
    st.subheader("Connection")
    connection_type = st.selectbox("Type", ["stdio", "sse"], index=0)
    server_url = st.text_input(
        "SSE URL",
        value="http://127.0.0.1:8000/sse",
        help="Used only for SSE connections.",
    )
    server_reference = st.text_input(
        "Server Module",
        value=config.mcp_config.server_reference,
        help="Python module for stdio MCP server.",
    )

prompt = st.text_area(
    "Prompt",
    value=st.session_state.last_prompt,
    height=180,
    placeholder="Describe what you want the agent to do...",
)

run_clicked = st.button("Run", type="primary")


def normalize_output(text: str) -> str:
    if not text:
        return ""
    cleaned = text.strip()
    if cleaned.startswith("{"):
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return str(parsed.get("output") or parsed.get("error") or cleaned)
        except json.JSONDecodeError:
            return cleaned
    return cleaned


def friendly_from_event(event: dict) -> list[str]:
    messages = []
    event_type = event.get("type")
    tool = event.get("tool", "")
    args = event.get("args") or {}
    result = normalize_output(str(event.get("result") or ""))
    tool_lower = tool.lower()

    def clean_path(path_value: str) -> str:
        if not path_value:
            return "the requested path"
        return path_value.replace("\\\\", "\\")

    if event_type == "tool_start":
        if tool_lower.endswith("planning"):
            messages.append("Updating the plan.")
            return messages
        if "str_replace" in tool_lower:
            command = str(args.get("command") or "edit")
            path = clean_path(str(args.get("path") or "a file"))
            if command == "create":
                messages.append(f"Creating a new file at {path}.")
            elif command == "replace":
                messages.append(f"Updating content in {path}.")
            elif command == "insert":
                messages.append(f"Inserting content into {path}.")
            else:
                messages.append(f"Editing {path}.")
        elif "browser_use" in tool_lower:
            action = args.get("action")
            if action == "web_search":
                query = args.get("query") or "your query"
                messages.append(f"Searching the web for {query}.")
            elif action == "navigate":
                url = args.get("url") or "a page"
                messages.append(f"Opening {url}.")
            else:
                messages.append("Browsing to collect information.")
        else:
            clean_tool = tool.split("_")[-1].replace("-", " ")
            messages.append(f"Running {clean_tool} to move forward.")
        return messages

    if event_type == "tool_result":
        if tool_lower.endswith("planning"):
            messages.append("Plan updated.")
            return messages
        if "str_replace" in tool_lower:
            if "not an absolute path" in result.lower():
                messages.append(
                    "That file path is not absolute. Use a full path like "
                    "C:\\Users\\A-plus\\OpenManus\\workspace\\your-file.txt."
                )
            elif result.lower().startswith("error executing tool"):
                messages.append("I could not write the file. I will try again with a valid path.")
            else:
                messages.append("File update complete.")
            return messages
        if result.lower().startswith("search results for"):
            lines = [line.strip() for line in result.splitlines()[1:] if line.strip()]
            messages.append(f"Found {len(lines)} results and will open a few.")
        elif result.lower().startswith("navigated to"):
            url = result.split(" ", 2)[-1].strip()
            messages.append(f"Opened {url}.")
        elif "no content was extracted" in result.lower():
            messages.append("That page blocked extraction. Trying another source.")
        elif "verify you are human" in result.lower() or "captcha" in result.lower():
            messages.append("That site needs human verification, skipping it.")
        elif "security" in result.lower() and "review" in result.lower():
            messages.append("That site needs a security check, skipping it.")
        else:
            if result:
                messages.append("Found useful content and will summarize it.")
            else:
                messages.append("Continuing the investigation.")
        return messages

    if event_type == "tool_error":
        if "str_replace" in tool_lower:
            messages.append("I could not update the file. I will try a different path.")
        else:
            messages.append("Hit a snag and will try a different approach.")
        return messages

    return messages


def extract_findings(result: str) -> list[str]:
    findings = []
    if result.lower().startswith("search results for"):
        lines = [line.strip() for line in result.splitlines()[1:] if line.strip()]
        for line in lines[:6]:
            findings.append(line)
    elif "extracted content" in result.lower():
        findings.append(result)
    return findings


def parse_plan_output(text: str) -> dict | None:
    if not text:
        return None
    plan_id = None
    title = None
    steps: list[str] = []
    statuses: list[str] = []
    notes: list[str] = []
    in_steps = False
    for line in text.splitlines():
        clean = line.strip()
        if clean.startswith("Plan: "):
            match = re.match(r"Plan:\\s*(.+)\\s*\\(ID:\\s*(.+)\\)", clean)
            if match:
                title = match.group(1).strip()
                plan_id = match.group(2).strip()
            continue
        if clean == "Steps:":
            in_steps = True
            continue
        if not in_steps:
            continue
        step_match = re.match(r"^(\\d+)\\.\\s+\\[(.*?)\\]\\s+(.*)$", clean)
        if step_match:
            status_token = (step_match.group(2) or "").strip().lower()
            step_text = step_match.group(3).strip()
            if "!" in status_token:
                status = "blocked"
            elif "x" in status_token:
                status = "completed"
            elif ">" in status_token or "~" in status_token:
                status = "in_progress"
            elif status_token:
                status = "in_progress"
            else:
                status = "not_started"
            steps.append(step_text)
            statuses.append(status)
            notes.append("")
            continue
        if clean.startswith("Notes:") and notes:
            notes[-1] = clean.split("Notes:", 1)[1].strip()
    if not steps:
        return None
    return {
        "plan_id": plan_id or "",
        "title": title or "Plan",
        "steps": steps,
        "step_statuses": statuses,
        "step_notes": notes,
    }


def get_active_step_index(plan_data: dict) -> int | None:
    if not plan_data:
        return None
    statuses = plan_data.get("step_statuses") or []
    for idx, status in enumerate(statuses):
        if status == "in_progress":
            return idx
    for idx, status in enumerate(statuses):
        if status == "blocked":
            return idx
    for idx, status in enumerate(statuses):
        if status == "not_started":
            return idx
    return None


def render_plan(container, plan_data: dict | None) -> None:
    if not plan_data:
        container.markdown(
            "<div class='plan-step'>No plan yet.</div>", unsafe_allow_html=True
        )
        return
    steps = plan_data.get("steps") or []
    statuses = plan_data.get("step_statuses") or []
    notes = plan_data.get("step_notes") or []
    active_index = get_active_step_index(plan_data)
    title = html.escape(plan_data.get("title") or "Plan")
    if active_index is None:
        current_text = "All steps complete."
    else:
        current_text = f"Step {active_index + 1}: {steps[active_index]}"
    header = (
        f"<div class='plan-step'><strong>{title}</strong>"
        f"<div class='plan-meta'>Current: {html.escape(current_text)}</div></div>"
    )
    cards = []
    for idx, step in enumerate(steps):
        status = statuses[idx] if idx < len(statuses) else "not_started"
        status_label = {
            "not_started": "Not started",
            "in_progress": "In progress",
            "completed": "Completed",
            "blocked": "Blocked",
        }.get(status, "Not started")
        note_text = notes[idx] if idx < len(notes) else ""
        note_html = (
            f"<div class='plan-meta'>Notes: {html.escape(note_text)}</div>"
            if note_text
            else ""
        )
        active_class = " active" if active_index == idx else ""
        cards.append(
            "<div class='plan-step{active_class}'>"
            "<span class='status'>{status}</span>"
            "{step}"
            "{notes}"
            "</div>".format(
                active_class=active_class,
                status=html.escape(status_label),
                step=html.escape(f"{idx + 1}. {step}"),
                notes=note_html,
            )
        )
    container.markdown(f"{header}<div class='plan'>{''.join(cards)}</div>", unsafe_allow_html=True)


def start_agent_thread(
    user_prompt: str,
    connection: str,
    sse_url: str,
    module_ref: str,
    event_queue: Queue,
    result_queue: Queue,
) -> threading.Thread:
    async def _run() -> str:
        agent = MCPAgent()
        agent.event_sink = lambda evt: event_queue.put(evt)
        if connection == "stdio":
            await agent.initialize(
                connection_type="stdio",
                command=sys.executable,
                args=["-m", module_ref],
            )
        else:
            await agent.initialize(connection_type="sse", server_url=sse_url)
        return await agent.run(user_prompt)

    def runner() -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(_run())
            result_queue.put((True, result))
        except Exception as exc:
            result_queue.put((False, exc, traceback.format_exc()))
        finally:
            try:
                loop.close()
            except Exception:
                pass

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return thread


def run_summary_thread(findings: list[str]) -> str:
    async def _run() -> str:
        llm = LLM()
        prompt_text = "\n".join(findings)
        user_prompt = (
            "Write a detailed, user-friendly summary based on the findings. "
            "Use short paragraphs or bullet points, include concrete tips, "
            "and avoid technical jargon. If sources disagree, note that."
        )
        return await llm.ask(
            messages=[{"role": "user", "content": f"{user_prompt}\n\n{prompt_text}"}],
            stream=False,
        )

    result_queue: "Queue[tuple[bool, object]]" = Queue()

    def runner() -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result_queue.put((True, loop.run_until_complete(_run())))
        except Exception as exc:
            result_queue.put((False, exc))
        finally:
            try:
                loop.close()
            except Exception:
                pass

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    ok, payload = result_queue.get()
    if ok:
        return str(payload)
    return "I could not generate a summary."


def render_steps(container, steps: list[str]) -> None:
    if not steps:
        container.markdown("<div class='step'>Waiting for updates...</div>", unsafe_allow_html=True)
        return
    cards = "\n".join([f"<div class='step'>{step}</div>" for step in steps])
    container.markdown(f"<div class='steps'>{cards}</div>", unsafe_allow_html=True)


def add_story_line(line: str) -> None:
    if not line:
        return
    recent = st.session_state.live_story[-6:]
    if line in recent:
        return
    st.session_state.live_story.append(line)


def event_key(event: dict) -> str:
    event_type = event.get("type", "")
    tool = event.get("tool", "")
    args = event.get("args") or {}
    result = normalize_output(str(event.get("result") or ""))
    key_parts = [event_type, tool]
    if isinstance(args, dict):
        key_parts.append(str(args.get("action") or ""))
        key_parts.append(str(args.get("query") or ""))
        key_parts.append(str(args.get("url") or ""))
    if result:
        key_parts.append(result[:80])
    return "|".join(key_parts)


if run_clicked:
    cleaned_prompt = prompt.strip()
    if not cleaned_prompt:
        st.warning("Please enter a prompt.")
    else:
        st.session_state.last_prompt = cleaned_prompt
        st.session_state.last_error = ""
        st.session_state.live_story = ["Starting now."]
        st.session_state.live_findings = []
        st.session_state.summary_text = ""
        st.session_state.blocked_by_captcha = False
        st.session_state.plan_data = None

        event_queue: "Queue[dict]" = Queue()
        result_queue: "Queue[tuple]" = Queue()
        thread = start_agent_thread(
            cleaned_prompt,
            connection_type,
            server_url,
            server_reference,
            event_queue,
            result_queue,
        )

        status_box = st.empty()
        plan_box = st.empty()
        findings_box = st.empty()

        render_plan(plan_box, st.session_state.plan_data)

        with st.spinner("Working on it..."):
            while thread.is_alive() or not event_queue.empty():
                updated = False
                while not event_queue.empty():
                    event = event_queue.get()
                    if event.get("type") == "tool_result":
                        result = normalize_output(str(event.get("result") or ""))
                        if "planning" in str(event.get("tool", "")).lower() or "Plan:" in result:
                            plan_data = parse_plan_output(result)
                            if plan_data:
                                st.session_state.plan_data = plan_data
                                updated = True
                    key = event_key(event)
                    if key in st.session_state.story_keys:
                        continue
                    st.session_state.story_keys.add(key)
                    for line in friendly_from_event(event):
                        add_story_line(line)
                        updated = True
                    if event.get("type") == "tool_result":
                        result = normalize_output(str(event.get("result") or ""))
                        if "captcha" in result.lower() or "verify you are human" in result.lower():
                            st.session_state.blocked_by_captcha = True
                        for finding in extract_findings(result):
                            if finding not in st.session_state.live_findings:
                                st.session_state.live_findings.append(finding)
                                updated = True
                if updated:
                    render_steps(status_box, st.session_state.live_story)
                    render_plan(plan_box, st.session_state.plan_data)
                    if st.session_state.live_findings:
                        findings_box.markdown(
                            "\n".join([f"- {line}" for line in st.session_state.live_findings])
                        )
                time.sleep(0.1)

        ok_payload = result_queue.get()
        if ok_payload[0]:
            st.session_state.live_story.append("Done.")
        else:
            st.session_state.last_error = ok_payload[2]
            st.session_state.live_story.append("I ran into a problem.")

        render_steps(status_box, st.session_state.live_story)
        render_plan(plan_box, st.session_state.plan_data)

        if st.session_state.live_findings:
            st.session_state.summary_text = run_summary_thread(st.session_state.live_findings)

if st.session_state.last_prompt:
    st.markdown("<div class='bubble'><strong>You:</strong></div>", unsafe_allow_html=True)
    st.write(st.session_state.last_prompt)


st.subheader("Findings")
if st.session_state.live_findings:
    st.markdown("\n".join([f"- {line}" for line in st.session_state.live_findings]))
else:
    if st.session_state.blocked_by_captcha:
        st.info("Some sites required human verification. Try a different query or source.")
    else:
        st.info("No readable content yet. Try a different prompt or source.")

if st.session_state.summary_text:
    st.subheader("Summary")
    st.markdown(f"<div class='summary'>{st.session_state.summary_text}</div>", unsafe_allow_html=True)
