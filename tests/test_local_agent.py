"""Tests of cadprompt/local_agent.py against a fake OpenAI server. Plain python3:

    python3 tests/test_local_agent.py
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from cadprompt import local_agent                      # noqa: E402
from cadprompt.local_agent import Agent, AgentError, Stopped   # noqa: E402
from local_fake_server import FakeServer, answer, call, error, slow   # noqa: E402

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


class Guards(unittest.TestCase):
    """The FreeCAD tests write keys and settings. Without FREECAD_USER_HOME that would be
    the user's real FreeCAD profile, so each of them must stop before it does anything."""

    FILES = ("test_backends.py", "test_local_tools.py", "test_local_engine.py", "test_panel.py",
             "test_panel_engine.py", "test_integration_gui.py", "test_drawing_headless.py",
             "test_drawing_gui.py", "local_smoke.py")

    def test_freecad_tests_refuse_to_run_without_a_scratch_profile(self):
        env = {k: v for k, v in os.environ.items() if k != "FREECAD_USER_HOME"}
        for name in self.FILES:
            # plain Python: even a script without the guard could not reach FreeCAD from here
            done = subprocess.run([sys.executable, os.path.join(HERE, name)], env=env,
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 2, "%s: %s" % (name, (done.stdout + done.stderr)[-300:]))
            self.assertNotIn("Traceback", done.stdout + done.stderr, name)


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
