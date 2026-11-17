# RUN: python
"""Tests of cadprompt/local_agent.py against a fake OpenAI server. Plain python3:

    python3 tests/test_local_agent.py
"""

import base64
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from utf8_output import utf8_output            # noqa: E402
import fake_proxy                              # noqa: E402

utf8_output()

from cadprompt import local_agent                      # noqa: E402
from cadprompt.local_agent import Agent, AgentError, Stopped   # noqa: E402
from local_fake_server import (FakeAnthropic, FakeServer, answer, call, claude, claude_error,   # noqa: E402
                               cut, error, slow)

TOOLS = [{"type": "function", "function": {"name": "run_python", "description": "run",
                                           "parameters": {"type": "object", "properties": {}}}}]
OK = "CHANGES:\n+ new Plate: Part::Box, size 100.000 x 50.000 x 10.000"
BAD = "ERROR:\nTraceback (most recent call last):\nNameError: name 'x' is not defined\nCHANGES:\nno object changed"


class Base(unittest.TestCase):
    def setUp(self):
        self.srv = FakeServer()
        self.events = []
        self.tool_calls = []
        self.results = {}

    def tearDown(self):
        self.srv.close()

    def run_tool(self, name, args):
        self.tool_calls.append((name, args))
        r = self.results.get(name, OK)
        return r.pop(0) if isinstance(r, list) else r

    def agent(self, **kw):
        kw.setdefault("system", "SYSTEM")
        kw.setdefault("tools", TOOLS)
        kw.setdefault("timeout", 10)
        return Agent(self.srv.url, kw.pop("model", "fake-model"), self.run_tool,
                     on_event=lambda k, d: self.events.append((k, d)), **kw)

    def kinds(self):
        return [k for k, _ in self.events if k != "state"]

    def sent(self, n):
        return self.srv.requests[n]["body"]["messages"]


class Loop(Base):
    def test_tool_call_then_result_then_answer(self):
        self.srv.add(call("run_python", {"code": "make()"}, "c1"), answer("Plate 100 x 50 x 10 is ready."))
        a = self.agent()
        out = a.ask("Make a plate.")
        self.assertEqual(out, "Plate 100 x 50 x 10 is ready.")
        self.assertEqual(self.tool_calls, [("run_python", {"code": "make()"})])
        self.assertEqual(self.kinds(), ["tool_call", "tool_result", "answer"])
        self.assertEqual(len(self.srv.requests), 2)
        first, second = self.srv.requests[0]["body"], self.srv.requests[1]["body"]
        self.assertEqual(self.srv.requests[0]["path"], "/v1/chat/completions")
        self.assertEqual(first["model"], "fake-model")
        self.assertEqual(first["tools"], TOOLS)
        self.assertEqual(first["max_tokens"], 6000)
        self.assertEqual([m["role"] for m in first["messages"]], ["system", "user"])
        self.assertEqual(first["messages"][0]["content"], "SYSTEM")
        self.assertEqual([m["role"] for m in second["messages"]], ["system", "user", "assistant", "tool"])
        self.assertEqual(second["messages"][3], {"role": "tool", "tool_call_id": "c1", "content": OK})
        self.assertEqual(second["messages"][2]["tool_calls"][0]["id"], "c1")
        self.assertEqual([m["role"] for m in a.messages], ["user", "assistant", "tool", "assistant"])

    def test_error_result_sends_the_model_back(self):
        self.results["run_python"] = [BAD]
        self.results["get_document_state"] = 'Document "Model", 1 objects:\n- Plate: Part::Box'
        self.srv.add(call("run_python", {"code": "x"}), answer("Done."),
                     call("get_document_state", {}), answer("Now it is correct."))
        a = self.agent()
        out = a.ask("Make a plate.")
        self.assertEqual(out, "Now it is correct.")
        self.assertEqual(len(self.srv.requests), 4)
        pushed = self.sent(2)[-1]
        self.assertEqual(pushed["role"], "user")
        self.assertTrue(pushed["content"].startswith("Automatic check"))
        self.assertIn("NameError", pushed["content"])
        self.assertEqual(self.kinds(), ["tool_call", "tool_result", "text", "note",
                                        "tool_call", "tool_result", "answer"])
        self.assertTrue(self.events[[k for k, _ in self.events].index("tool_result")][1]["problem"])
        autos = [m for m in a.messages if m.get("auto")]
        self.assertEqual(len(autos), 1)

    def test_at_most_two_pushes(self):
        self.results["run_python"] = [BAD]
        self.srv.add(call("run_python", {"code": "x"}), answer("Done."), answer("Done, really."),
                     answer("It is done."), answer("never asked for"))
        a = self.agent()
        out = a.ask("Make a plate.")
        self.assertEqual(len(self.srv.requests), 4)
        self.assertTrue(out.startswith("It is done."))
        self.assertIn("still shows a problem", out)
        self.assertEqual(len([m for m in a.messages if m.get("auto")]), 2)

    def test_problem_objects_line_counts_as_problem(self):
        self.assertTrue(local_agent.find_problem("run_python", "CHANGES:\n+ new Cut\nPROBLEM OBJECTS: Cut"))
        self.assertTrue(local_agent.find_problem("make_drawing", "ERROR: no object to draw"))
        self.assertFalse(local_agent.find_problem("run_python", OK))
        # a line that starts with ERROR inside a file the model reads is data, not a failure
        self.assertFalse(local_agent.find_problem("read_project_file", "FILE notes.md:\nERROR codes of the machine"))

    def test_printed_text_is_not_a_problem(self):
        # run_python indents what the code printed: only the tool's own lines count
        printed = "OUTPUT:\n  ERROR margin 0.2 mm is inside tolerance\n  PROBLEM OBJECTS: none\nCHANGES:\nno object changed"
        self.assertFalse(local_agent.find_problem("run_python", printed))
        self.assertFalse(local_agent.find_problem("run_python", "OUTPUT:\n  ERRORS: 0\nCHANGES:\n+ new Plate"))
        self.assertTrue(local_agent.find_problem("run_python", "OUTPUT:\n  start\nERROR:\nTraceback\nNameError"))
        # only the tool's own two headings count, not every line that starts with the word
        self.assertFalse(local_agent.find_problem("run_python", "CHANGES:\nERROR-proof plate\nPROBLEM OBJECTS"))
        self.assertTrue(local_agent.find_problem("get_document_state", "Document:\n- Cut\nPROBLEM OBJECTS: Cut"))

    def test_drawing_or_file_error_does_not_start_the_repair_check(self):
        # "no solid to draw" and "no such file" are for the user: there is nothing to repair
        for name, text in (("make_drawing", "ERROR: the document 'Model' has no solid to draw."),
                           ("read_project_file", "ERROR: there is no file refs/none.md in the project.")):
            self.setUp()
            self.results[name] = text
            self.srv.add(call(name, {}), answer("There is nothing to draw."), answer("never asked for"))
            a = self.agent()
            out = a.ask("/drawing A3")
            self.assertEqual(out, "There is nothing to draw.", name)       # no "still shows a problem"
            self.assertEqual(len(self.srv.requests), 2, name)              # and no extra model requests
            self.assertEqual([m for m in a.messages if m.get("auto")], [], name)
            self.assertFalse(a.incomplete, name)
            # the panel still marks the tool as failed
            self.assertTrue([d for k, d in self.events if k == "tool_result"][0]["problem"], name)
            self.tearDown()
        self.setUp()

    def test_a_good_drawing_does_not_clear_a_model_problem(self):
        self.results["run_python"] = [BAD]
        self.results["make_drawing"] = "Drawing made."
        self.srv.add(call("run_python", {"code": "x"}), call("make_drawing", {}), answer("Done."),
                     answer("Done."), answer("Done."))
        a = self.agent()
        self.assertIn("still shows a problem", a.ask("Make a plate and draw it."))
        self.assertTrue(a.incomplete)

    def test_incomplete_is_set_only_when_the_work_did_not_end_well(self):
        self.srv.add(call("run_python", {"code": "a"}), answer("Done."))
        a = self.agent()
        a.ask("x")
        self.assertFalse(a.incomplete)
        self.srv.add(*[call("run_python", {"code": "n=%d" % i}, "c%d" % i) for i in range(5)])
        b = self.agent(max_steps=2)
        b.ask("Loop")
        self.assertTrue(b.incomplete)                       # the step limit

    def test_a_text_that_the_server_cut_off_is_not_a_good_end(self):
        self.srv.add(call("run_python", {"code": "a"}), cut(answer("Now I add the four ho")))
        a = self.agent()
        out = a.ask("x")
        self.assertTrue(out.startswith("Now I add the four ho"))
        self.assertIn("cut off", out)
        self.assertTrue(a.incomplete)                       # the engine does not save the model then
        self.assertNotIn("_cut_off", json.dumps(a.messages))
        self.assertNotIn("_cut_off", json.dumps(self.srv.bodies()))

    def test_only_thinking_before_the_limit_still_gets_the_nudge(self):
        self.srv.add(cut(answer("", reasoning="I think and think")), answer("Done."))
        a = self.agent()
        self.assertEqual(a.ask("x"), "Done.")
        self.assertFalse(a.incomplete)
        self.assertEqual(len(self.srv.requests), 2)

    def test_a_tool_call_at_the_limit_is_run_when_it_is_whole(self):
        self.srv.add(cut(call("run_python", {"code": "a"})), answer("Done."))
        a = self.agent()
        self.assertEqual(a.ask("x"), "Done.")
        self.assertEqual(self.tool_calls, [("run_python", {"code": "a"})])
        self.assertFalse(a.incomplete)

    def test_empty_answer_gets_one_nudge(self):
        self.srv.add(answer("", reasoning="thinking a lot"), answer("Hello."))
        a = self.agent()
        self.assertEqual(a.ask("Hi"), "Hello.")
        self.assertEqual(len(self.srv.requests), 2)
        second = self.sent(1)
        # roles still alternate: the nudge is merged into the user message
        self.assertEqual([m["role"] for m in second], ["system", "user"])
        self.assertIn("Hi", second[1]["content"])
        self.assertIn("Your last answer was empty", second[1]["content"])
        self.assertEqual(self.kinds(), ["note", "answer"])

    def test_two_empty_answers_end_cleanly(self):
        self.srv.add(answer(""), answer(None), answer("never asked for"))
        a = self.agent()
        out = a.ask("Hi")
        self.assertEqual(len(self.srv.requests), 2)
        self.assertIn("no final answer", out)
        self.assertEqual(a.messages[-1], {"role": "assistant", "content": out})

    def test_the_same_failed_call_is_not_run_again(self):
        self.results["run_python"] = [BAD, OK, OK]
        same = {"code": "x"}
        self.srv.add(call("run_python", same, "c1"), call("run_python", same, "c2"),
                     call("run_python", same, "c3"), call("run_python", {"code": "y"}, "c4"),
                     call("run_python", {"code": "y"}, "c5"), answer("Done."))
        a = self.agent()
        self.assertEqual(a.ask("Make a plate."), "Done.")
        # run 1 failed; 2 and 3 are the same call and are refused; 4 is new and works; 5 repeats a
        # call that worked, which is allowed
        self.assertEqual([args["code"] for _, args in self.tool_calls], ["x", "y", "y"])
        results = [m["content"] for m in a.messages if m["role"] == "tool"]
        self.assertEqual(results[0], BAD)
        for r in results[1:3]:
            self.assertTrue(r.startswith("ERROR: this is exactly the same call"), r)
            self.assertIn("NameError", r)
        # the second refusal quotes the error of run 1, not the first refusal
        self.assertEqual(results[2], results[1])
        self.assertEqual(results[2].count("exactly the same call"), 1)
        self.assertEqual(results[3:], [OK, OK])
        self.assertEqual(self.kinds().count("tool_call"), 5)       # the panel still sees every call

    def test_step_limit(self):
        self.srv.add(*[call("run_python", {"code": "n=%d" % i}, "c%d" % i) for i in range(10)])
        a = self.agent(max_steps=3)
        out = a.ask("Loop")
        self.assertEqual(len(self.srv.requests), 3)
        self.assertIn("step limit (3)", out)

    def test_bad_tool_arguments(self):
        bad = call("run_python", {})
        bad["tool_calls"][0]["function"]["arguments"] = "{not json"
        self.srv.add(bad, answer("Sorry."), answer("Stop now."), answer("Stop."))
        a = self.agent()
        a.ask("x")
        self.assertEqual(self.tool_calls, [])
        tool_msg = [m for m in a.messages if m["role"] == "tool"][0]
        self.assertIn("not valid JSON", tool_msg["content"])

    def test_arguments_as_object_and_missing_id(self):
        odd = {"role": "assistant", "content": None,
               "tool_calls": [{"function": {"name": "run_python", "arguments": {"code": "a=1"}}}]}
        self.srv.add(odd, answer("ok"))
        a = self.agent()
        a.ask("x")
        self.assertEqual(self.tool_calls, [("run_python", {"code": "a=1"})])
        sent_call = self.sent(1)[2]["tool_calls"][0]
        self.assertEqual(sent_call["function"]["arguments"], '{"code": "a=1"}')
        self.assertEqual(self.sent(1)[3]["tool_call_id"], sent_call["id"])
        self.assertTrue(sent_call["id"])


class Wire(Base):
    def test_reasoning_content_is_never_sent_back(self):
        self.srv.add(call("run_python", {"code": "a"}, reasoning="SECRET-THOUGHT one"),
                     answer("<think>SECRET-THOUGHT two</think>Done.", reasoning="SECRET-THOUGHT three"),
                     answer("Second answer.", reasoning="SECRET-THOUGHT four"))
        a = self.agent()
        self.assertEqual(a.ask("first"), "Done.")
        self.assertEqual(a.ask("second"), "Second answer.")
        self.assertEqual(len(self.srv.requests), 3)
        wire = json.dumps(self.srv.bodies())
        self.assertNotIn("reasoning_content", wire)
        self.assertNotIn("SECRET-THOUGHT", wire)
        self.assertNotIn("SECRET-THOUGHT", json.dumps(a.messages))
        for body in self.srv.bodies():
            for m in body["messages"]:
                self.assertLessEqual(set(m), {"role", "content", "tool_calls", "tool_call_id"})

    def test_private_fields_stay_private(self):
        self.srv.add(answer("ok"))
        a = self.agent()
        a.ask("expanded text of the command", shown="/drawing A3")
        self.assertEqual(a.messages[0]["shown"], "/drawing A3")
        self.assertEqual(self.sent(0)[1], {"role": "user", "content": "expanded text of the command"})

    def test_no_authorization_header_without_key(self):
        self.srv.add(answer("ok"))
        self.agent().ask("x")
        headers = {k.lower(): v for k, v in self.srv.requests[0]["headers"].items()}
        self.assertNotIn("authorization", headers)

    def test_authorization_header_with_key(self):
        self.srv.add(answer("ok"))
        self.agent(key="sk-test-123").ask("x")
        headers = {k.lower(): v for k, v in self.srv.requests[0]["headers"].items()}
        self.assertEqual(headers.get("authorization"), "Bearer sk-test-123")


class Address(unittest.TestCase):
    def url(self, text, key=""):
        return Agent(text, "m", None, key=key).url

    def test_no_scheme_with_a_key_is_https(self):
        # a key must never go out in clear text because "https://" was not typed
        self.assertEqual(self.url("openrouter.ai/api/v1", "sk-1"), "https://openrouter.ai/api/v1")
        self.assertEqual(self.url("api.openai.com/v1/", "sk-1"), "https://api.openai.com/v1")
        self.assertEqual(self.url("192.168.1.50:1234/v1", "sk-1"), "https://192.168.1.50:1234/v1")

    def test_no_scheme_on_this_computer_or_without_a_key_is_http(self):
        self.assertEqual(self.url("localhost:1234/v1", "sk-1"), "http://localhost:1234/v1")
        self.assertEqual(self.url("127.0.0.1:1234/v1", "sk-1"), "http://127.0.0.1:1234/v1")
        self.assertEqual(self.url("[::1]:1234/v1", "sk-1"), "http://[::1]:1234/v1")
        self.assertEqual(self.url("192.168.1.50:1234/v1"), "http://192.168.1.50:1234/v1")

    def test_a_typed_scheme_is_kept(self):
        self.assertEqual(self.url("http://192.168.1.50:1234/v1", "sk-1"), "http://192.168.1.50:1234/v1")
        self.assertEqual(self.url("https://openrouter.ai/api/v1"), "https://openrouter.ai/api/v1")


class Errors(Base):
    def tearDown(self):
        super().tearDown()
        local_agent._REFUSED.clear()

    def strict(self, body):
        """A server like OpenAI's newer models: the old limit name and a temperature are refused."""
        if "max_tokens" in body:
            return error(400, "Unsupported parameter: 'max_tokens' is not supported with this model. "
                              "Use 'max_completion_tokens' instead.")
        if "temperature" in body:
            return error(400, "Unsupported value: 'temperature' does not support 0.2 with this model. "
                              "Only the default (1) value is supported.")
        return answer("ok")

    def test_refused_request_fields_are_left_out(self):
        self.srv.add(self.strict, self.strict, self.strict, self.strict)
        a = self.agent()
        self.assertEqual(a.ask("x"), "ok")
        fields = [sorted(k for k in b if k in ("max_tokens", "max_completion_tokens", "temperature"))
                  for b in self.srv.bodies()]
        self.assertEqual(fields, [["max_tokens", "temperature"], ["max_completion_tokens", "temperature"],
                                  ["max_completion_tokens"]])
        self.assertEqual(self.srv.bodies()[2]["max_completion_tokens"], 6000)
        # a new Agent for the same server and model does not ask the two questions again
        self.assertEqual(self.agent().ask("y"), "ok")
        self.assertEqual(len(self.srv.requests), 4)
        self.assertNotIn("max_tokens", self.srv.bodies()[3])

    def test_a_server_that_refuses_for_ever_ends_with_its_error(self):
        text = "Unsupported parameter: 'max_tokens' and 'temperature' are not supported."
        self.srv.add(*[error(400, text)] * 6)
        with self.assertRaises(AgentError) as cm:
            self.agent().ask("x")
        self.assertIn("HTTP 400", str(cm.exception))
        self.assertEqual(len(self.srv.requests), 3)

    def test_http_401_is_a_clear_error(self):
        self.srv.add(error(401, "Incorrect API key provided"))
        a = self.agent(key="wrong")
        with self.assertRaises(AgentError) as cm:
            a.ask("x")
        self.assertIn("API key", str(cm.exception))
        self.assertIn("401", str(cm.exception))
        self.assertEqual(a.messages, [])     # the failed request is not kept
        self.assertEqual(len(self.srv.requests), 1)

    def test_http_403(self):
        self.srv.add(error(403, "forbidden"))
        with self.assertRaises(AgentError) as cm:
            self.agent().ask("x")
        self.assertIn("API key", str(cm.exception))

    def test_unreachable_server(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                             # nothing listens on this port now
        a = Agent("http://127.0.0.1:%d/v1" % port, "m", self.run_tool, timeout=5)
        t0 = time.time()
        with self.assertRaises(AgentError) as cm:
            a.ask("x")
        self.assertIn("does not answer", str(cm.exception))
        self.assertIn("Start the server", str(cm.exception))
        self.assertLess(time.time() - t0, 3)
        self.assertEqual(a.messages, [])

    def test_unknown_host(self):
        a = Agent("http://no-such-host.invalid:1234/v1", "m", self.run_tool, timeout=5)
        with self.assertRaises(AgentError) as cm:
            a.ask("x")
        self.assertIn("does not answer", str(cm.exception))

    def test_server_timeout(self):
        self.srv.add(slow(5, answer("late")))
        a = self.agent(timeout=0.5)
        t0 = time.time()
        with self.assertRaises(AgentError) as cm:
            a.ask("x")
        self.assertIn("did not answer within", str(cm.exception))
        self.assertLess(time.time() - t0, 3)

    def test_model_not_found(self):
        self.srv.add(error(404, 'Model "nope" not found', "model_not_found"))
        with self.assertRaises(AgentError) as cm:
            self.agent(model="nope").ask("x")
        self.assertIn('does not have the model "nope"', str(cm.exception))
        self.assertIn("Pick a model", str(cm.exception))

    def test_wrong_address_404(self):
        self.srv.add(("error", 404, {"detail": "Not Found"}))
        with self.assertRaises(AgentError) as cm:
            self.agent().ask("x")
        self.assertIn("not the address of an OpenAI-compatible server", str(cm.exception))

    def test_no_model_set(self):
        with self.assertRaises(AgentError) as cm:
            self.agent(model="").ask("x")
        self.assertIn("No model is set", str(cm.exception))
        self.assertEqual(self.srv.requests, [])

    def test_context_overflow_is_a_clear_error(self):
        text = "The number of tokens to keep from the initial prompt is greater than the context length."
        self.srv.add(error(400, text), error(400, text))
        with self.assertRaises(AgentError) as cm:
            self.agent().ask("x")
        self.assertIn("too long for this model", str(cm.exception))
        self.assertEqual(len(self.srv.requests), 2)      # one retry with less, then give up

    def test_context_overflow_retries_with_less(self):
        self.results["run_python"] = "OUTPUT:\n" + "y" * 3000
        self.srv.add(call("run_python", {"code": "a"}, "c1"), call("run_python", {"code": "b"}, "c2"),
                     call("run_python", {"code": "c"}, "c3"),
                     error(400, "This model's maximum context length is 4096 tokens."),
                     answer("Done."))
        a = self.agent(context_chars=100000)
        self.assertEqual(a.ask("x"), "Done.")
        full, less = self.srv.bodies()[3], self.srv.bodies()[4]
        self.assertLess(len(json.dumps(less)), len(json.dumps(full)) * 0.7)

    def test_other_http_error_shows_the_server_text(self):
        self.srv.add(error(500, "GPU ran out of memory"))
        with self.assertRaises(AgentError) as cm:
            self.agent().ask("x")
        self.assertIn("HTTP 500", str(cm.exception))
        self.assertIn("GPU ran out of memory", str(cm.exception))

    def test_not_an_openai_server(self):
        self.srv.add(("error", 200, {"hello": "world"}))
        with self.assertRaises(AgentError) as cm:
            self.agent().ask("x")
        self.assertIn("was not understood", str(cm.exception))

    def test_error_after_progress_keeps_the_conversation(self):
        self.srv.add(call("run_python", {"code": "a"}), error(500, "boom"))
        a = self.agent()
        with self.assertRaises(AgentError):
            a.ask("x")
        self.assertEqual([m["role"] for m in a.messages], ["user", "assistant", "tool"])


class Stop(Base):
    def test_stop_between_steps(self):
        self.srv.add(call("run_python", {"code": "a"}), call("run_python", {"code": "b"}), answer("no"))
        a = self.agent()

        def run_tool(name, args):
            a.stop()
            return OK
        a.run_tool = run_tool
        with self.assertRaises(Stopped):
            a.ask("x")
        self.assertEqual(len(self.srv.requests), 1)     # no further request after the stop
        self.assertTrue(a.stopped())

    def test_no_second_tool_after_stop(self):
        two = call("run_python", {"code": "a"}, "c1")
        two["tool_calls"].append({"id": "c2", "type": "function",
                                  "function": {"name": "run_python", "arguments": '{"code": "b"}'}})
        self.srv.add(two, answer("no"))
        a = self.agent()
        ran = []

        def run_tool(name, args):
            ran.append(args["code"])
            a.stop()
            return OK
        a.run_tool = run_tool
        with self.assertRaises(Stopped):
            a.ask("x")
        self.assertEqual(ran, ["a"])

    def test_stop_before_start(self):
        self.srv.add(answer("no"))
        a = self.agent()
        a.stop()
        with self.assertRaises(Stopped):
            a.ask("x")
        self.assertEqual(self.srv.requests, [])

    def test_stop_ends_a_waiting_request(self):
        self.srv.add(slow(8, answer("late")))
        a = self.agent(timeout=30)
        threading.Timer(0.3, a.stop).start()
        t0 = time.time()
        with self.assertRaises(Stopped):
            a.ask("x")
        self.assertLess(time.time() - t0, 2)
        time.sleep(0.3)
        self.assertEqual(self.srv.dropped, 1)            # the server saw the client hang up


class Shortening(Base):
    def test_long_conversation_is_shortened_on_the_wire_only(self):
        big = "OUTPUT:\n" + "\n".join("line %04d of a long result" % i for i in range(120))
        self.results["run_python"] = big
        code = "\n".join("step_%04d = make_something(%d)" % (i, i) for i in range(40))
        self.srv.add(*[call("run_python", {"code": code}, "c%d" % i) for i in range(6)])
        self.srv.add(answer("Done."))
        a = self.agent(context_chars=14000)
        self.assertEqual(a.ask("Build it."), "Done.")
        last = self.srv.bodies()[-1]["messages"]
        tools = [m for m in last if m["role"] == "tool"]
        self.assertEqual(len(tools), 6)
        self.assertIn("(shortened", tools[0]["content"])
        self.assertLess(len(tools[0]["content"]), 400)
        self.assertEqual(tools[-1]["content"], big)            # the newest result is complete
        self.assertEqual(tools[-2]["content"], big)
        # old code is cut too, and stays valid JSON
        old_call = [m for m in last if m["role"] == "assistant"][0]["tool_calls"][0]
        self.assertIn("(shortened", json.loads(old_call["function"]["arguments"])["code"])
        new_call = [m for m in last if m["role"] == "assistant"][-1]["tool_calls"][0]
        self.assertEqual(json.loads(new_call["function"]["arguments"])["code"], code)
        self.assertLess(len(json.dumps(last)), 14000)
        self.assertGreater(len(json.dumps(a.messages)), 25000)
        # the stored conversation is complete
        stored = [m for m in a.messages if m["role"] == "tool"]
        self.assertTrue(all(m["content"] == big for m in stored))
        # a short conversation is sent as it is
        self.assertNotIn("(shortened", json.dumps(self.srv.bodies()[1]))

    def test_old_requests_are_left_out_whole(self):
        msgs = []
        for n in range(6):
            msgs.append({"role": "user", "content": "request %d " % n + "u" * 300})
            msgs.append({"role": "assistant", "content": "", "tool_calls": [
                {"id": "c%d" % n, "type": "function",
                 "function": {"name": "run_python", "arguments": json.dumps({"code": "c" * 500})}}]})
            msgs.append({"role": "tool", "tool_call_id": "c%d" % n, "content": "r" * 800})
            msgs.append({"role": "assistant", "content": "answer %d " % n + "a" * 300})
        msgs.append({"role": "user", "content": "request 6"})
        view, dropped = local_agent.build_view(msgs, 3000)
        self.assertTrue(dropped)
        self.assertEqual(view[0]["role"], "user")                 # starts at a request of the user
        self.assertEqual(view[-1]["content"], "request 6")
        self.assertNotIn("request 0", json.dumps(view))
        self.assertLessEqual(len(json.dumps(view)), 3000)
        # every tool result still has its call before it
        seen = set()
        for m in view:
            for c in m.get("tool_calls") or []:
                seen.add(c["id"])
            if m["role"] == "tool":
                self.assertIn(m["tool_call_id"], seen)
        self.assertEqual(len(msgs), 25)                           # the stored list is untouched
        self.assertEqual(msgs[2]["content"], "r" * 800)
        full, dropped = local_agent.build_view(msgs, 10 ** 6)
        self.assertFalse(dropped)
        self.assertEqual(len(full), 25)

    def test_the_current_request_is_never_left_out(self):
        msgs = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "now " + "n" * 5000}]
        view, dropped = local_agent.build_view(msgs, 1000)
        self.assertTrue(dropped)
        self.assertEqual(len(view), 1)
        self.assertTrue(view[0]["content"].startswith("now "))

    def test_left_out_start_is_told_to_the_model(self):
        self.srv.add(answer("ok"))
        old = [{"role": "user", "content": "old " + "o" * 9000}, {"role": "assistant", "content": "old answer"}]
        a = self.agent(messages=old, context_chars=4000)
        a.ask("new request")
        sent = self.sent(0)
        self.assertIn("left out", sent[0]["content"])
        self.assertEqual([m["role"] for m in sent], ["system", "user"])
        self.assertEqual(len(a.messages), 4)


SIGNATURE_ERROR = ("messages.1.content.0: Invalid `signature` in `thinking` block. The block is bound "
                   "to a different conversation. Remove the block.")
PNG = ("image/png", "QUJD")          # what load_image gives in these tests: no real picture is needed


def roles(body):
    return [m["role"] for m in body["messages"]]


class AnthropicBase(Base):
    """The same loop against the Anthropic Messages API (a scripted server)."""

    def setUp(self):
        super().setUp()
        self.srv.close()
        self.srv = FakeAnthropic()

    def agent(self, **kw):
        kw.setdefault("kind", "anthropic")
        kw.setdefault("key", "sk-ant-made-up")
        kw.setdefault("model", "claude-fake-1")
        return super().agent(**kw)

    def well_formed(self):
        """What the Messages API refuses: two turns of one role in a row, an empty text."""
        for body in self.srv.bodies():
            self.assertEqual(roles(body)[0], "user")
            self.assertEqual(roles(body)[-1], "user")
            for one, two in zip(roles(body), roles(body)[1:]):
                self.assertNotEqual(one, two)
            for m in body["messages"]:
                self.assertTrue(m["content"])
                for block in m["content"]:
                    if block["type"] == "text":
                        self.assertTrue(block["text"].strip())


class AnthropicLoop(AnthropicBase):
    def test_tool_round_trip(self):
        self.srv.add(claude("I make it.", [("run_python", {"code": "make()"}, "toolu_1")]),
                     claude("Plate 100 x 50 x 10 is ready."))
        a = self.agent()
        self.assertEqual(a.ask("Make a plate."), "Plate 100 x 50 x 10 is ready.")
        self.assertEqual(self.tool_calls, [("run_python", {"code": "make()"})])
        self.assertEqual(self.kinds(), ["text", "tool_call", "tool_result", "answer"])
        first, second = self.srv.bodies()
        self.assertEqual(self.srv.requests[0]["path"], "/v1/messages")
        headers = {k.lower(): v for k, v in self.srv.requests[0]["headers"].items()}
        self.assertEqual(headers.get("x-api-key"), "sk-ant-made-up")
        self.assertEqual(headers.get("anthropic-version"), "2026-10-21")
        self.assertNotIn("authorization", headers)
        self.assertEqual(first["model"], "claude-fake-1")
        self.assertEqual(first["max_tokens"], 16000)             # the API does not answer without it
        self.assertEqual(first["system"], "SYSTEM")              # not a message, as in the OpenAI format
        self.assertNotIn("temperature", first)                   # this API version refuses it
        self.assertEqual(first["tools"], [{"name": "run_python", "description": "run",
                                           "input_schema": {"type": "object", "properties": {}}}])
        self.assertEqual(first["messages"], [{"role": "user", "content": [{"type": "text", "text": "Make a plate."}]}])
        self.assertEqual(second["messages"][1:], [
            {"role": "assistant", "content": [
                {"type": "text", "text": "I make it."},
                {"type": "tool_use", "id": "toolu_1", "name": "run_python", "input": {"code": "make()"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": OK}]}])
        # the stored conversation is the neutral one, the same as with an OpenAI server
        self.assertEqual(a.messages, [
            {"role": "user", "content": "Make a plate."},
            {"role": "assistant", "content": "I make it.", "tool_calls": [
                {"id": "toolu_1", "type": "function",
                 "function": {"name": "run_python", "arguments": json.dumps({"code": "make()"})}}]},
            {"role": "tool", "tool_call_id": "toolu_1", "content": OK},
            {"role": "assistant", "content": "Plate 100 x 50 x 10 is ready."}])
        self.well_formed()

    def test_tool_error_goes_back_as_an_error_and_the_check_follows(self):
        self.results["run_python"] = [BAD, OK]
        self.srv.add(claude("", [("run_python", {"code": "x"}, "t1")]), claude("Done."),
                     claude("", [("run_python", {"code": "y"}, "t2")]), claude("Now it is right."))
        a = self.agent()
        self.assertEqual(a.ask("Make a plate."), "Now it is right.")
        bodies = self.srv.bodies()
        result = bodies[1]["messages"][-1]["content"][0]
        self.assertEqual((result["type"], result["tool_use_id"], result.get("is_error")), ("tool_result", "t1", True))
        self.assertIn("NameError", result["content"])
        # "Done." with a broken model: the automatic check is one more user turn
        self.assertEqual(roles(bodies[2]), ["user", "assistant", "user", "assistant", "user"])
        self.assertIn("Automatic check", bodies[2]["messages"][-1]["content"][0]["text"])
        good = bodies[3]["messages"][-1]["content"][0]
        self.assertEqual((good["tool_use_id"], good.get("is_error")), ("t2", None))
        self.assertFalse(a.incomplete)
        self.well_formed()

    def test_two_calls_in_one_answer_get_their_results_in_one_turn(self):
        self.srv.add(claude("", [("run_python", {"code": "a"}, "t1"), ("get_document_state", {}, "t2")]),
                     claude("Done."))
        self.agent().ask("x")
        last = self.srv.bodies()[1]["messages"][-1]
        self.assertEqual([(b["type"], b["tool_use_id"]) for b in last["content"]],
                         [("tool_result", "t1"), ("tool_result", "t2")])
        self.well_formed()

    def test_an_empty_answer_gets_one_nudge(self):
        self.srv.add(claude(""), claude("Here I am."))
        a = self.agent()
        self.assertEqual(a.ask("x"), "Here I am.")
        self.assertIn("empty", self.srv.bodies()[1]["messages"][-1]["content"][-1]["text"])
        self.well_formed()

    def test_thinking_goes_back_unchanged_and_is_never_stored(self):
        self.srv.add(claude("", [("run_python", {"code": "a"}, "t1")], thinking="one"),
                     claude("", [("run_python", {"code": "b"}, "t2")], thinking="two"),
                     claude("Done.", thinking="three"), claude("Second answer."))
        a = self.agent()
        self.assertEqual(a.ask("first"), "Done.")
        third = self.srv.bodies()[2]["messages"]
        # in one request the signed blocks go back as they came, in their place
        self.assertEqual(third[1]["content"][0], {"type": "thinking", "thinking": "", "signature": "SIGNATURE-one"})
        self.assertEqual(third[3]["content"][0], {"type": "thinking", "thinking": "", "signature": "SIGNATURE-two"})
        # they are not in what is stored: not in a state event, not in the messages as they are saved
        states = [d for k, d in self.events if k == "state"]
        self.assertNotIn("SIGNATURE", json.dumps(states))
        self.assertNotIn("_native", json.dumps(states))
        stored = json.loads(json.dumps(local_agent.public(a.messages)))
        b = self.agent(messages=stored)
        self.assertEqual(b.ask("second"), "Second answer.")
        self.assertNotIn("thinking", json.dumps(self.srv.bodies()[3]["messages"]))
        self.well_formed()

    def test_refused_thinking_blocks_are_left_out_and_the_run_goes_on(self):
        self.srv.add(claude("", [("run_python", {"code": "a"}, "t1")], thinking="one"),
                     claude_error(400, "invalid_request_error", SIGNATURE_ERROR), claude("Done."))
        a = self.agent()
        self.assertEqual(a.ask("x"), "Done.")
        self.assertIn("SIGNATURE-one", json.dumps(self.srv.bodies()[1]))
        again = self.srv.bodies()[2]
        self.assertNotIn("thinking", json.dumps(again["messages"]))
        self.assertEqual(again["messages"][1]["content"],
                         [{"type": "tool_use", "id": "t1", "name": "run_python", "input": {"code": "a"}}])

    def test_an_answer_that_was_cut_off_runs_no_tool(self):
        self.srv.add(claude("I start.", [("run_python", {"code": "half a prog"}, "t1")], stop_reason="max_tokens"))
        a = self.agent()
        out = a.ask("x")
        self.assertEqual(self.tool_calls, [])
        self.assertIn("I start.", out)
        self.assertIn("cut off", out)
        self.assertTrue(a.incomplete)                       # the engine does not save the model then
        self.assertNotIn("_cut_off", json.dumps(a.messages))

    def test_an_answer_that_was_cut_off_after_tools_ran_is_incomplete(self):
        self.srv.add(claude("", [("run_python", {"code": "a"}, "t1")]),
                     claude("", [("run_python", {"code": "half"}, "t2")], stop_reason="max_tokens"))
        a = self.agent()
        out = a.ask("x")
        self.assertEqual(self.tool_calls, [("run_python", {"code": "a"})])
        self.assertIn("cut off", out)
        self.assertTrue(a.incomplete)
        self.srv.add(claude("", [("run_python", {"code": "a"}, "t1")]), claude("Done."))
        b = self.agent()
        b.ask("x")
        self.assertFalse(b.incomplete)                      # a normal end stays a good end

    def test_a_refusal_is_a_plain_error(self):
        self.srv.add(claude("", stop_reason="refusal"))
        a = self.agent()
        with self.assertRaises(AgentError) as cm:
            a.ask("x")
        self.assertIn("declined", str(cm.exception))
        self.assertEqual(a.messages, [])


class AnthropicErrors(AnthropicBase):
    def setUp(self):
        super().setUp()
        self.backoff, local_agent.BACKOFF = local_agent.BACKOFF, 0.05

    def tearDown(self):
        local_agent.BACKOFF = self.backoff
        super().tearDown()

    def fails(self, *replies, **kw):
        self.srv.add(*replies)
        a = self.agent(**kw)
        with self.assertRaises(AgentError) as cm:
            a.ask("x")
        self.assertEqual(a.messages, [])           # nothing happened: the request is forgotten
        self.assertNotIn("local model", str(cm.exception))
        self.assertNotIn("model server", str(cm.exception))
        return str(cm.exception)

    def test_bad_key(self):
        text = self.fails(claude_error(401, "authentication_error", "invalid x-api-key"))
        self.assertIn("did not accept the API key", text)
        self.assertNotIn("sk-ant-made-up", text)
        self.assertEqual(len(self.srv.requests), 1)

    def test_no_credit(self):
        text = self.fails(claude_error(400, "invalid_request_error",
                                       "Your credit balance is too low to access the Anthropic API."))
        self.assertIn("no credit", text)
        self.assertEqual(len(self.srv.requests), 1)

    def test_billing_error(self):
        self.assertIn("no credit", self.fails(claude_error(402, "billing_error", "Payment is required.")))

    def test_permission(self):
        text = self.fails(claude_error(403, "permission_error", "Not allowed for this workspace."))
        self.assertIn("not allowed", text)
        self.assertIn("Not allowed for this workspace.", text)

    def test_model_not_found(self):
        text = self.fails(claude_error(404, "not_found_error", "model: claude-nope"), model="claude-nope")
        self.assertIn('does not have the model "claude-nope"', text)
        self.assertIn("Find models", text)

    def test_context_too_long(self):
        long = claude_error(400, "invalid_request_error", "prompt is too long: 1200000 tokens > 1000000 maximum")
        text = self.fails(long, long)
        self.assertIn("too long for this model", text)
        self.assertIn("new chat", text)
        self.assertEqual(len(self.srv.requests), 2)      # one retry with less, then give up

    def test_request_too_large(self):
        big = claude_error(413, "request_too_large", "Request exceeds the maximum allowed number of bytes.")
        self.assertIn("too long for this model", self.fails(big, big))

    def test_other_error_shows_what_anthropic_said(self):
        text = self.fails(claude_error(400, "invalid_request_error", "tools.0.input_schema: bad"))
        self.assertIn("HTTP 400", text)
        self.assertIn("tools.0.input_schema: bad", text)

    def test_overloaded_waits_and_tries_again(self):
        self.srv.add(claude_error(529, "overloaded_error", "Overloaded"), claude("Done."))
        a = self.agent()
        self.assertEqual(a.ask("x"), "Done.")
        self.assertEqual(len(self.srv.requests), 2)
        self.assertTrue(any(k == "note" and "busy" in d for k, d in self.events))

    def test_overloaded_for_too_long_is_a_plain_error(self):
        over = claude_error(529, "overloaded_error", "Overloaded")
        text = self.fails(over, over, over, over)
        self.assertIn("overloaded", text)
        self.assertEqual(len(self.srv.requests), 1 + local_agent.RETRIES)

    def test_rate_limit_waits_for_retry_after(self):
        self.srv.add(claude_error(429, "rate_limit_error", "Too many requests", {"retry-after": "1"}),
                     claude("Done."))
        a = self.agent()
        t0 = time.time()
        self.assertEqual(a.ask("x"), "Done.")
        self.assertGreaterEqual(time.time() - t0, 0.9)
        self.assertLess(time.time() - t0, 5)
        notes = [d for k, d in self.events if k == "note"]
        self.assertEqual(len(notes), 1)
        self.assertIn("wait 1 seconds", notes[0])
        self.assertEqual(len(self.srv.requests), 2)

    def test_a_very_long_wait_is_the_users_decision(self):
        t0 = time.time()
        text = self.fails(claude_error(429, "rate_limit_error", "Too many requests", {"retry-after": "600"}))
        self.assertLess(time.time() - t0, 3)
        self.assertIn("rate limit", text)
        self.assertEqual(len(self.srv.requests), 1)

    def test_stop_ends_the_wait_for_the_rate_limit(self):
        self.srv.add(claude_error(429, "rate_limit_error", "Too many requests", {"retry-after": "30"}))
        a = self.agent()
        threading.Timer(0.3, a.stop).start()
        t0 = time.time()
        with self.assertRaises(Stopped):
            a.ask("x")
        self.assertLess(time.time() - t0, 3)
        self.assertEqual(len(self.srv.requests), 1)

    def test_stop_ends_a_waiting_request(self):
        self.srv.add(slow(8, claude("late")))
        a = self.agent(timeout=30)
        threading.Timer(0.3, a.stop).start()
        t0 = time.time()
        with self.assertRaises(Stopped):
            a.ask("x")
        self.assertLess(time.time() - t0, 2)
        time.sleep(0.3)
        self.assertEqual(self.srv.dropped, 1)            # the server saw the client hang up

    def test_no_connection(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        a = Agent("http://127.0.0.1:%d" % port, "claude-fake-1", self.run_tool, key="k", kind="anthropic", timeout=5)
        with self.assertRaises(AgentError) as cm:
            a.ask("x")
        self.assertIn("No connection to Anthropic", str(cm.exception))

    def dead_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def no_connection(self, address, proxies):
        """The error text for a service that cannot be reached, on a computer with these proxy settings."""
        names = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
        clean = {k: v for k, v in os.environ.items() if k.lower() not in names}
        # without a variable, macOS and Windows read the settings of the system: none there
        system = mock.patch.object(urllib.request, "getproxies", return_value={})
        with mock.patch.dict(os.environ, dict(clean, **proxies), clear=True), (mock.MagicMock() if proxies else system):
            a = Agent(address, "claude-fake-1", self.run_tool, key="k", kind="anthropic", timeout=5)
            with self.assertRaises(AgentError) as cm:
                a.ask("x")
        self.assertIn("No connection to Anthropic", str(cm.exception))
        return str(cm.exception)

    def test_no_connection_on_a_network_with_a_proxy_says_that_the_two_ways_were_tried(self):
        https = "https://127.0.0.1:%d" % self.dead_port()
        proxy = {"https_proxy": "http://127.0.0.1:%d" % self.dead_port()}
        self.assertTrue(self.no_connection(https, proxy).endswith(local_agent.PROXY))
        self.assertIn("tried the proxy and then a direct connection", local_agent.PROXY)
        # no proxy in the settings, an exception for this address, a local http server: no such hint
        self.assertNotIn("proxy", self.no_connection(https, {}))
        self.assertNotIn("proxy", self.no_connection(https, dict(proxy, no_proxy="127.0.0.1")))
        http = https.replace("https", "http")
        self.assertNotIn("proxy", self.no_connection(http, {"http_proxy": proxy["https_proxy"]}))

    def test_an_http_server_is_never_reached_through_a_proxy(self):
        proxy = socket.socket()                 # it would be seen here: a connection waits in the queue
        proxy.bind(("127.0.0.1", 0))
        proxy.listen(5)
        proxy.settimeout(0.2)
        address = "http://127.0.0.1:%d" % proxy.getsockname()[1]
        self.srv.add(claude("direct"))
        names = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
        clean = {k: v for k, v in os.environ.items() if k.lower() not in names}
        with mock.patch.dict(os.environ, dict(clean, http_proxy=address, https_proxy=address), clear=True):
            self.assertEqual(self.agent().ask("x"), "direct")
        with self.assertRaises(OSError):        # nobody knocked
            proxy.accept()
        proxy.close()

    def test_not_the_messages_api(self):
        text = self.fails(("error", 200, {"hello": "world"}))
        self.assertIn("HTTP 200", text)


class RateLimitOpenAI(Base):
    def test_an_openai_server_with_a_rate_limit_is_waited_for_too(self):
        self.srv.add(error(429, "Rate limit reached", headers={"Retry-After": "1"}), answer("Done."))
        a = self.agent()
        t0 = time.time()
        self.assertEqual(a.ask("x"), "Done.")
        self.assertGreaterEqual(time.time() - t0, 0.9)
        self.assertTrue(any(k == "note" and "wait 1 seconds" in d for k, d in self.events))


class Images(Base):
    """Images go with the user message in the format of each service; only paths are stored."""

    def setUp(self):
        super().setUp()
        self.claude = FakeAnthropic()
        self.loaded = []

    def tearDown(self):
        self.claude.close()
        super().tearDown()

    def load(self, path):
        self.loaded.append(path)
        if "gone" in path:
            raise OSError("no such file")
        return PNG

    def agent(self, **kw):
        kw.setdefault("load_image", self.load)
        return super().agent(**kw)

    def anthropic(self, **kw):
        return Agent(self.claude.url, "claude-fake-1", self.run_tool, key="sk-ant-made-up", kind="anthropic",
                     system="SYSTEM", tools=TOOLS, timeout=10, load_image=self.load,
                     on_event=lambda k, d: self.events.append((k, d)), **kw)

    def test_openai_format(self):
        self.srv.add(call("run_python", {"code": "a"}, "c1"), answer("A plate with two holes."))
        a = self.agent()
        a.ask("What is this?", images=["refs/sketch.png", "refs/photo.jpg"])
        url = "data:image/png;base64,QUJD"
        for body in self.srv.bodies():           # every step of the request sends the images again
            self.assertEqual(body["messages"][1], {"role": "user", "content": [
                {"type": "text", "text": "What is this?"},
                {"type": "image_url", "image_url": {"url": url}},
                {"type": "image_url", "image_url": {"url": url}}]})
        self.assertEqual(self.loaded, ["refs/sketch.png", "refs/photo.jpg"])      # each file is read once
        self.assertEqual(a.messages[0], {"role": "user", "content": "What is this?",
                                         "images": ["refs/sketch.png", "refs/photo.jpg"]})
        self.assertNotIn("QUJD", json.dumps(a.messages))
        self.assertNotIn("QUJD", json.dumps([d for k, d in self.events if k == "state"]))

    def test_anthropic_format(self):
        self.claude.add(claude("A plate with two holes."))
        a = self.anthropic()
        a.ask("What is this?", images=["refs/sketch.png"])
        self.assertEqual(self.claude.bodies()[0]["messages"], [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
            {"type": "text", "text": "What is this?"}]}])
        self.assertEqual(a.messages[0]["images"], ["refs/sketch.png"])
        self.assertNotIn("QUJD", json.dumps(a.messages))

    def test_at_most_four_images_with_one_message(self):
        self.srv.add(answer("ok"))
        a = self.agent()
        a.ask("x", images=["refs/%d.png" % i for i in range(6)])
        parts = self.sent(0)[1]["content"]
        self.assertEqual(len([p for p in parts if p["type"] == "image_url"]), local_agent.MAX_IMAGES)
        self.assertEqual(a.messages[0]["images"], ["refs/2.png", "refs/3.png", "refs/4.png", "refs/5.png"])

    def test_a_file_that_is_no_image_is_a_plain_error_and_nothing_is_sent(self):
        a = self.agent()
        with self.assertRaises(AgentError) as cm:
            a.ask("x", images=["refs/gone.png"])
        self.assertIn("refs/gone.png could not be read", str(cm.exception))
        self.assertEqual(self.srv.connections, 0)
        self.assertEqual(a.messages, [])

    def test_an_old_image_whose_file_is_gone_becomes_a_line_of_text(self):
        stored = [{"role": "user", "content": "Look.", "images": ["refs/gone.png"]},
                  {"role": "assistant", "content": "A bracket."}]
        for srv, a, reply in ((self.srv, self.agent(messages=stored), answer("ok")),
                              (self.claude, self.anthropic(messages=stored), claude("ok"))):
            srv.add(reply)
            a.ask("And now?")
            first = srv.bodies()[0]["messages"]
            said = json.dumps(first)
            self.assertIn("An image was attached here: refs/gone.png", said)
            self.assertNotIn("image_url", said)
            self.assertNotIn('"type": "image"', said)

    def test_old_images_are_left_out_first_when_the_conversation_is_long(self):
        msgs = [{"role": "user", "content": "first", "images": ["refs/old.png"]},
                {"role": "assistant", "content": "A " * 200},
                {"role": "user", "content": "second", "images": ["refs/new.png"]}]
        view, dropped = local_agent.build_view(msgs, 100000, image_chars=1000)
        self.assertEqual([m.get("images") for m in view], [["refs/old.png"], None, ["refs/new.png"]])
        view, dropped = local_agent.build_view(msgs, 1800, image_chars=1000)
        self.assertFalse(dropped)                                  # no text had to go
        self.assertEqual([m.get("images") for m in view], [None, None, ["refs/new.png"]])
        self.assertIn("An image was attached here: refs/old.png", view[0]["content"])
        self.assertEqual(view[1]["content"], "A " * 200)
        self.assertEqual(msgs[0]["images"], ["refs/old.png"])     # the stored conversation is whole

    def test_a_model_without_vision_is_a_plain_hint_and_the_user_decides(self):
        self.srv.add(error(400, "Model does not support images. Please use a model that does."),
                     answer("Tell me the sizes."))
        a = self.agent()
        with self.assertRaises(local_agent.NoVision) as cm:
            a.ask("What is this?", images=["refs/sketch.png"])
        self.assertIn("This model cannot read images", str(cm.exception))
        self.assertEqual(a.messages, [])
        self.assertEqual(len(self.srv.requests), 1)                # not sent again by itself
        self.assertEqual(a.ask("What is this?"), "Tell me the sizes.")
        self.assertEqual(self.sent(1)[1], {"role": "user", "content": "What is this?"})

    def test_images_of_earlier_messages_do_not_block_a_model_without_vision(self):
        stored = [{"role": "user", "content": "Look.", "images": ["refs/sketch.png"]},
                  {"role": "assistant", "content": "A bracket."}]
        self.srv.add(error(400, "image input is not supported - hint: you may need to provide the mmproj"),
                     answer("Still a bracket."))
        a = self.agent(messages=stored)
        self.assertEqual(a.ask("And now?"), "Still a bracket.")
        self.assertIn("image_url", json.dumps(self.srv.bodies()[0]))
        again = json.dumps(self.srv.bodies()[1])
        self.assertNotIn("image_url", again)
        self.assertIn("An image was attached here: refs/sketch.png", again)
        self.assertTrue(any(k == "note" and "cannot read images" in d for k, d in self.events))
        self.assertEqual(a.messages[0]["images"], ["refs/sketch.png"])     # still stored


class Switch(Base):
    """One stored conversation, two services: a project can change its source in the middle."""

    def setUp(self):
        super().setUp()
        self.claude = FakeAnthropic()

    def tearDown(self):
        self.claude.close()
        super().tearDown()

    def anthropic(self, messages):
        return Agent(self.claude.url, "claude-fake-1", self.run_tool, key="sk-ant-made-up", kind="anthropic",
                     system="SYSTEM", tools=TOOLS, timeout=10, messages=messages)

    @staticmethod
    def stored(agent):
        return json.loads(json.dumps(local_agent.public(agent.messages)))      # as read back from the file

    def test_openai_then_anthropic_then_openai(self):
        # 1: a local server; its call id has letters the Messages API does not take
        self.srv.add(call("run_python", {"code": "plate()"}, "call:1/a", text="I start."), answer("A plate."))
        a = self.agent()
        a.ask("Make a plate.")
        # 2: the same conversation goes on with Anthropic
        self.claude.add(claude("One hole.", [("run_python", {"code": "hole()"}, "toolu_9")], thinking="x"),
                        claude("Plate with a hole."))
        b = self.anthropic(self.stored(a))
        self.assertEqual(b.ask("Add a hole."), "Plate with a hole.")
        first = self.claude.bodies()[0]["messages"]
        self.assertEqual(first, [
            {"role": "user", "content": [{"type": "text", "text": "Make a plate."}]},
            {"role": "assistant", "content": [
                {"type": "text", "text": "I start."},
                {"type": "tool_use", "id": "call_1_a", "name": "run_python", "input": {"code": "plate()"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1_a", "content": OK}]},
            {"role": "assistant", "content": [{"type": "text", "text": "A plate."}]},
            {"role": "user", "content": [{"type": "text", "text": "Add a hole."}]}])
        # 3: and back to the local server, with what Anthropic did
        self.srv.add(answer("Yes, one hole."))
        c = self.agent(messages=self.stored(b))
        self.assertEqual(c.ask("Is there a hole?"), "Yes, one hole.")
        sent = self.srv.bodies()[2]["messages"]
        self.assertEqual([m["role"] for m in sent], ["system", "user", "assistant", "tool", "assistant",
                                                     "user", "assistant", "tool", "assistant", "user"])
        self.assertEqual(sent[6], {"role": "assistant", "content": "One hole.", "tool_calls": [
            {"id": "toolu_9", "type": "function",
             "function": {"name": "run_python", "arguments": json.dumps({"code": "hole()"})}}]})
        self.assertEqual(sent[7], {"role": "tool", "tool_call_id": "toolu_9", "content": OK})
        for m in sent:
            self.assertLessEqual(set(m), {"role", "content", "tool_calls", "tool_call_id"})
        self.assertNotIn("SIGNATURE", json.dumps(self.srv.bodies()))
        self.assertNotIn("thinking", json.dumps(self.srv.bodies()))

    def uses_and_results(self, body):
        """(the tool_use ids, the tool_result ids) of a request to the Messages API, in order."""
        blocks = [(m["role"], b) for m in body["messages"] for b in m["content"]]
        return ([b["id"] for role, b in blocks if b["type"] == "tool_use"],
                [b["tool_use_id"] for role, b in blocks if b["type"] == "tool_result"])

    def test_a_server_that_sends_no_call_ids_does_not_block_the_change_to_anthropic(self):
        # a new Agent for each message, as the engine makes it: the made-up id is "call_1" both times
        no_id = {"role": "assistant", "content": "", "tool_calls": [
            {"type": "function", "function": {"name": "run_python", "arguments": "{}"}}]}
        stored = []
        for text in ("Make a plate.", "Add a hole."):
            self.srv.add(no_id, answer("Done."))
            a = self.agent(messages=stored)
            a.ask(text)
            stored = self.stored(a)
        ids = [c["id"] for m in stored for c in m.get("tool_calls") or []]
        self.assertEqual(ids, ["call_1", "call_1"])         # as stored: what the local server gets back
        self.results["run_python"] = ["first result", "second result"]
        self.claude.add(claude("", [("run_python", {}, "toolu_9")]), claude("ok"))
        b = self.anthropic(stored)
        b.ask("Go on.")
        for body in self.claude.bodies():
            uses, results = self.uses_and_results(body)
            self.assertEqual(len(set(uses)), len(uses), uses)       # the Messages API refuses a second use of an id
            self.assertEqual(results, uses)                         # and each result names the call before it
        self.assertEqual(self.uses_and_results(self.claude.bodies()[1])[0], ["call_1", "call_1_2", "toolu_9"])
        self.assertEqual([c["id"] for m in self.stored(b) for c in m.get("tool_calls") or []],
                         ["call_1", "call_1", "toolu_9"])           # the stored conversation is not rewritten

    def test_two_ids_that_differ_only_in_letters_anthropic_does_not_take(self):
        def use(ident):
            return {"id": ident, "type": "function", "function": {"name": "run_python", "arguments": "{}"}}
        view = [{"role": "user", "content": "x"},
                {"role": "assistant", "content": "", "tool_calls": [use("a:b"), use("a/b"), use("")]},
                {"role": "tool", "tool_call_id": "a/b", "content": "second"},
                {"role": "tool", "tool_call_id": "a:b", "content": "first"},
                {"role": "tool", "tool_call_id": "", "content": "third"},
                {"role": "assistant", "content": "", "tool_calls": [use("a_b"), use(None)]},
                {"role": "tool", "tool_call_id": "a_b", "content": "fourth"}]
        sent = local_agent.anthropic_messages(view, lambda path: None)
        self.assertEqual([b["id"] for b in sent[1]["content"]], ["a_b", "a_b_2", "call"])
        self.assertEqual([(b["tool_use_id"], b["content"]) for b in sent[2]["content"]],
                         [("a_b_2", "second"), ("a_b", "first"), ("call", "third")])
        self.assertEqual([b["id"] for b in sent[3]["content"]], ["a_b_3", "call_2"])
        self.assertEqual([(b["tool_use_id"], b["content"]) for b in sent[4]["content"][:2]],
                         [("a_b_3", "fourth"), ("call_2", "(no result)")])
        self.assertEqual(sent, local_agent.anthropic_messages(view, lambda path: None))   # the same each time

    def test_a_tool_name_the_local_model_made_up_goes_to_anthropic_in_safe_letters(self):
        no_tool = "ERROR: there is no tool %s. Use run_python."
        for made_up, safe in (("default_api.run_python", "default_api_run_python"),
                              ("run python", "run_python"), ("x" * 90, "x" * 64), ("", "tool")):
            stored = [{"role": "user", "content": "Make a plate."},
                      {"role": "assistant", "content": "", "tool_calls": [
                          {"id": "c1", "type": "function", "function": {"name": made_up, "arguments": "{}"}}]},
                      {"role": "tool", "tool_call_id": "c1", "content": no_tool % made_up},
                      {"role": "assistant", "content": "Sorry."}]
            self.claude.add(claude("ok"))
            b = self.anthropic(stored)
            b.ask("Go on.")
            sent = self.claude.bodies()[-1]["messages"]
            self.assertEqual(sent[1]["content"], [{"type": "tool_use", "id": "c1", "name": safe, "input": {}}])
            # the result still says which name the model asked for
            self.assertEqual(sent[2]["content"], [{"type": "tool_result", "tool_use_id": "c1",
                                                   "content": no_tool % made_up, "is_error": True}])
            # and the stored conversation is not changed: the local server gets its own name back
            self.assertEqual(self.stored(b)[1]["tool_calls"][0]["function"]["name"], made_up)

    def test_a_conversation_that_was_cut_in_a_step_is_made_whole_for_anthropic(self):
        stored = [{"role": "user", "content": "Make a plate."},
                  {"role": "assistant", "content": "", "tool_calls": [
                      {"id": "c1", "type": "function", "function": {"name": "run_python", "arguments": "{}"}}]},
                  {"role": "assistant", "content": "*Stopped.*"},
                  {"role": "tool", "tool_call_id": "lost", "content": "a result without its call"}]
        self.claude.add(claude("ok"))
        self.anthropic(stored).ask("Go on.")
        sent = self.claude.bodies()[0]["messages"]
        self.assertEqual([m["role"] for m in sent], ["user", "assistant", "user", "assistant", "user"])
        self.assertEqual(sent[2]["content"], [{"type": "tool_result", "tool_use_id": "c1", "content": "(no result)"}])
        self.assertEqual(sent[4]["content"][0]["type"], "text")       # never a result without a call
        self.assertIn("a result without its call", sent[4]["content"][0]["text"])

    def test_each_key_goes_to_its_own_service_only(self):
        self.srv.add(answer("local"))
        self.claude.add(claude("remote"))
        self.agent(key="LOCAL-KEY-made-up").ask("x")
        self.anthropic([]).ask("x")
        local = json.dumps(self.srv.requests)
        remote = json.dumps(self.claude.requests)
        self.assertIn("Bearer LOCAL-KEY-made-up", local)
        self.assertNotIn("sk-ant-made-up", local)
        self.assertNotIn("x-api-key", local.lower())
        self.assertIn("sk-ant-made-up", remote)
        self.assertNotIn("LOCAL-KEY-made-up", remote)
        self.assertNotIn("authorization", remote.lower())


class Gate(Base):
    """may_contact(kind, url) is asked before every request. False: no connection is opened."""

    def setUp(self):
        super().setUp()
        self.claude = FakeAnthropic()
        self.asked = []

    def tearDown(self):
        self.claude.close()
        super().tearDown()

    def gate(self, allow, counts):
        def may(kind, url):
            # what the servers have seen so far: the question comes before the request
            self.asked.append((kind, url, tuple(s.connections for s in counts)))
            return allow
        return may

    def test_asked_before_every_request(self):
        self.srv.add(call("run_python", {"code": "a"}, "c1"), call("run_python", {"code": "b"}, "c2"),
                     answer("Done."))
        self.agent(may_contact=self.gate(True, [self.srv])).ask("x")
        self.assertEqual(self.asked, [("openai", self.srv.url, (0,)), ("openai", self.srv.url, (1,)),
                                      ("openai", self.srv.url, (2,))])
        del self.asked[:]
        self.claude.add(claude("", [("run_python", {"code": "a"}, "t1")]), claude("Done."))
        Agent(self.claude.url, "claude-fake-1", self.run_tool, key="k", kind="anthropic", timeout=10,
              may_contact=self.gate(True, [self.claude])).ask("x")
        self.assertEqual(self.asked, [("anthropic", self.claude.url, (0,)), ("anthropic", self.claude.url, (1,))])

    def test_not_allowed_opens_no_socket(self):
        for srv, kind in ((self.srv, "openai"), (self.claude, "anthropic")):
            a = Agent(srv.url, "m", self.run_tool, key="KEY-made-up", kind=kind, timeout=10,
                      may_contact=self.gate(False, [srv]), load_image=lambda path: PNG)
            with self.assertRaises(AgentError) as cm:
                a.ask("x", images=["refs/a.png"])
            self.assertIn("Nothing was sent", str(cm.exception))
            time.sleep(0.2)
            self.assertEqual((srv.connections, srv.requests, srv.gets), (0, [], []))
            self.assertEqual(a.messages, [])
        self.assertEqual(len(self.asked), 2)


class Guards(unittest.TestCase):
    """The FreeCAD tests write keys and settings. Without FREECAD_USER_HOME that would be
    the user's real FreeCAD profile, so each of them must stop before it does anything."""

    FILES = ("test_backends.py", "test_local_tools.py", "test_local_engine.py", "test_panel.py",
             "test_panel_engine.py", "test_integration_gui.py", "test_drawing_headless.py",
             "test_drawing_gui.py", "local_smoke.py", "test_migrate.py", "test_bridge_gui.py",
             "claude_real_run.py")

    def test_freecad_tests_refuse_to_run_without_a_scratch_profile(self):
        env = {k: v for k, v in os.environ.items() if k != "FREECAD_USER_HOME"}
        for name in self.FILES:
            # plain Python: even a script without the guard could not reach FreeCAD from here
            done = subprocess.run([sys.executable, os.path.join(HERE, name)], env=env,
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 2, "%s: %s" % (name, (done.stdout + done.stderr)[-300:]))
            self.assertNotIn("Traceback", done.stdout + done.stderr, name)


class ConnectTimeout(Base):
    """Reaching the server has a short time limit of its own; the answer keeps the long one."""

    @staticmethod
    def hang(seen):
        def create_connection(address, timeout=None, source_address=None, **kw):
            seen.append(timeout)                 # a network that drops the packets: no answer, no refusal
            time.sleep(min(timeout, 5))
            raise TimeoutError("timed out")
        return create_connection

    def test_a_network_that_drops_the_packets_gives_its_error_soon(self):
        seen = []
        with mock.patch("socket.create_connection", self.hang(seen)):
            a = self.agent(timeout=600, connect_timeout=0.3)
            t0 = time.time()
            with self.assertRaises(AgentError) as cm:
                a.ask("x")
        self.assertEqual(seen, [0.3])
        self.assertLess(time.time() - t0, 3)
        self.assertIn("does not answer", str(cm.exception))
        self.assertIn("Start the server", str(cm.exception))
        self.assertNotIn("600", str(cm.exception))           # not "did not answer within 600 seconds"
        self.assertEqual(a.messages, [])

    def test_the_connection_gets_15_seconds_by_default_and_the_answer_600(self):
        seen = []
        with mock.patch("socket.create_connection", self.hang(seen)), mock.patch("time.sleep"):
            a = Agent(self.srv.url, "m", self.run_tool)
            with self.assertRaises(AgentError):
                a.ask("x")
        self.assertEqual((seen, local_agent.CONNECT_TIMEOUT, a.timeout), ([15], 15, 600))

    def test_a_short_time_limit_for_the_answer_is_not_made_longer(self):
        seen = []
        with mock.patch("socket.create_connection", self.hang(seen)), mock.patch("time.sleep"):
            with self.assertRaises(AgentError):
                self.agent(timeout=2).ask("x")
        self.assertEqual(seen, [2])

    def test_an_answer_can_take_longer_than_the_connection_may(self):
        self.srv.add(slow(1.2, answer("late, and fine")))
        a = self.agent(timeout=10, connect_timeout=0.3)
        self.assertEqual(a.ask("x"), "late, and fine")

    def test_anthropic_says_no_connection_not_slow_answer(self):
        seen = []
        with mock.patch("socket.create_connection", self.hang(seen)):
            a = Agent("http://127.0.0.1:9", "claude-fake-1", self.run_tool, key="k", kind="anthropic",
                      timeout=600, connect_timeout=0.2)
            with self.assertRaises(AgentError) as cm:
                a.ask("x")
        self.assertEqual(seen, [0.2])
        self.assertIn("No connection to Anthropic", str(cm.exception))
        self.assertNotIn("600", str(cm.exception))


class ThroughAProxy(unittest.TestCase):
    """local_agent.connect(), the one way out for the engine and for Find models, on a computer
    whose settings name a proxy. The service speaks real TLS (tests/fake_proxy.py)."""

    KEY = "sk-made-up-KEY-for-the-proxy-tests"

    def setUp(self):
        self.service = fake_proxy.Service()
        self.addCleanup(self.service.close)
        self.context = fake_proxy.client_context()

    def proxy(self, mode="pass"):
        proxy = fake_proxy.Proxy(mode)
        self.addCleanup(proxy.close)
        return proxy

    def get(self, setting, address=None):
        """One GET with the key, on a computer with this https proxy in its settings: (status, body)."""
        names = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
        clean = {k: v for k, v in os.environ.items() if k.lower() not in names}
        with mock.patch.dict(os.environ, dict(clean, https_proxy=setting), clear=True):
            conn = local_agent.connect(address or self.service.url, 10, context=self.context)
            try:
                conn.request("GET", "/v1/models", headers={"x-api-key": self.KEY})
                resp = conn.getresponse()
                return resp.status, resp.read()
            finally:
                conn.close()

    def test_https_goes_through_the_proxy_of_the_system_and_the_proxy_reads_nothing(self):
        proxy = self.proxy()
        status, body = self.get(proxy.url)
        self.assertEqual((status, json.loads(body)["data"][0]["id"]), (200, "model-1"))
        # the proxy got one request: where to connect to. No address of ours, no key.
        self.assertEqual([line for line, _ in proxy.requests], ["CONNECT 127.0.0.1:%d HTTP/1.1" % self.service.port])
        self.assertNotIn("proxy-authorization", proxy.requests[0][1])
        self.assertNotIn(self.KEY.encode(), proxy.clear)
        self.assertNotIn(b"/v1/models", proxy.clear)
        # the service got the request as without a proxy: its own name as Host, the key
        path, headers = self.service.seen[-1]
        self.assertEqual((path, headers["host"], headers["x-api-key"]),
                         ("/v1/models", "127.0.0.1:%d" % self.service.port, self.KEY))
        self.assertNotIn("proxy-authorization", headers)

    def test_the_certificate_of_the_service_is_checked_through_the_proxy_too(self):
        proxy = self.proxy()
        self.context = ssl.create_default_context()          # does not know the test certificate
        with self.assertRaises(ssl.SSLError):
            self.get(proxy.url)
        self.assertEqual(self.service.seen, [])

    def test_a_login_in_the_setting_goes_to_the_proxy_and_not_to_the_service(self):
        proxy = self.proxy()
        status, _ = self.get("http://zo%C3%AB:pa%3Ass@127.0.0.1:" + str(proxy.port))
        self.assertEqual(status, 200)
        expected = "Basic " + base64.b64encode("zo\u00eb:pa:ss".encode("utf-8")).decode("ascii")
        self.assertEqual(proxy.requests[0][1].get("proxy-authorization"), expected)
        self.assertNotIn("proxy-authorization", self.service.seen[-1][1])

    def test_a_proxy_that_does_not_let_us_through_is_followed_by_the_direct_way(self):
        for mode in ("login", "refuse"):
            proxy = self.proxy(mode)
            status, _ = self.get(proxy.url)
            self.assertEqual(status, 200, mode)
            self.assertEqual(len(proxy.requests), 1, mode)           # it was asked first
            self.assertNotIn(self.KEY.encode(), proxy.clear, mode)

    def test_a_proxy_setting_that_is_old_is_followed_by_the_direct_way(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
        s.close()
        self.assertEqual(self.get("http://127.0.0.1:%d" % dead)[0], 200)
        self.assertEqual(self.get("this is not an address")[0], 200)
        self.assertEqual(self.get("socks5://127.0.0.1:%d" % dead)[0], 200)

    def test_the_way_windows_writes_one_proxy_for_all_protocols(self):
        proxy = self.proxy()                                 # "https://host:port", a plain proxy all the same
        self.assertEqual(self.get("https://127.0.0.1:%d" % proxy.port)[0], 200)
        self.assertEqual(len(proxy.requests), 1)

    def test_an_exception_in_the_settings_is_obeyed(self):
        proxy = self.proxy()
        names = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
        clean = {k: v for k, v in os.environ.items() if k.lower() not in names}
        with mock.patch.dict(os.environ, dict(clean, https_proxy=proxy.url, no_proxy="127.0.0.1"), clear=True):
            self.assertIsNone(local_agent.proxy_for(self.service.url))
            self.assertIsNone(local_agent.proxy_for("http://example.invalid:1234/v1"))
        with mock.patch.dict(os.environ, dict(clean, https_proxy=proxy.url, http_proxy=proxy.url), clear=True):
            self.assertEqual(local_agent.proxy_for(self.service.url), ("127.0.0.1", proxy.port, {}))
            self.assertIsNone(local_agent.proxy_for("http://example.invalid:1234/v1"))   # http: never


class BrokenLine(Base):
    @unittest.skipUnless(hasattr(signal, "SIGPIPE"), "this system has no SIGPIPE")
    def test_a_write_to_a_closed_connection_is_an_error_not_the_end_of_the_program(self):
        # FreeCAD does not ignore SIGPIPE as Python does. With the signal as FreeCAD has it, a
        # request written to a connection that is closed (Stop, or a server that cut the line)
        # would end the whole program, and this test with it. The engine works in its own thread.
        before = signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        got = []

        def worker():
            conn = local_agent.connect(self.srv.url, 10)
            conn.sock.shutdown(socket.SHUT_RDWR)             # what Agent.stop() does from the other thread
            try:
                conn.request("POST", "/v1/chat/completions", b"x" * 100000, {"Content-Type": "application/json"})
                got.append("sent")
            except OSError as exc:
                got.append(exc)
            finally:
                conn.close()
        try:
            t = threading.Thread(target=worker)
            t.start()
            t.join(10)
        finally:
            signal.signal(signal.SIGPIPE, before)
        self.assertEqual(len(got), 1)
        self.assertIsInstance(got[0], OSError)


class Continue(Base):
    def test_a_new_agent_continues_the_stored_conversation(self):
        self.srv.add(answer("First answer."), answer("Second answer."))
        a = self.agent()
        a.ask("first question")
        stored = json.loads(json.dumps(a.messages))         # as if read back from a file
        b = self.agent(messages=stored)
        b.ask("second question")
        sent = self.sent(1)
        self.assertEqual([m["content"] for m in sent],
                         ["SYSTEM", "first question", "First answer.", "second question"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
