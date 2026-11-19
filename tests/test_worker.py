import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'worker' / 'agent.py'
spec = importlib.util.spec_from_file_location('factory_worker_test', MODULE)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class ProviderCliTests(unittest.TestCase):
    def test_provider_commands_and_permissions(self):
        def run(cmd, _cwd, **kwargs):
            if cmd[0] == 'claude':
                self.assertEqual(cmd[1], '-p')
                self.assertIn('--permission-prompts', cmd)
                self.assertEqual(cmd[cmd.index('--permission-mode')+1], 'plan')
                self.assertNotIn('--dangerously-skip-permissions', cmd)
                return SimpleNamespace(returncode=0, stdout='{"result":"ok Claude"}')
            if cmd[0] == 'gemini':
                self.assertEqual(cmd[cmd.index('--approval-mode')+1], 'plan')
                self.assertNotIn('yolo', cmd)
                return SimpleNamespace(returncode=0, stdout='{"response":"ok Gemini"}')
            raise AssertionError(cmd)

        with patch.object(agent, 'invoke', side_effect=run):
            for provider in ('claude', 'gemini'):
                code, text = agent.call_model(provider, 'reviewer', 'review task', agent.WORKSPACE)
                self.assertEqual(code, 0)
                self.assertTrue(text.startswith('ok'))

    def test_writer_auto_edits_not_full_bypass(self):
        def run(cmd, _cwd, **kwargs):
            if cmd[0] == 'claude':
                self.assertEqual(cmd[cmd.index('--permission-mode')+1], 'acceptEdits')
                return SimpleNamespace(returncode=0, stdout='{"result":"edits"}')
            if cmd[0] == 'gemini':
                self.assertEqual(cmd[cmd.index('--approval-mode')+1], 'auto_edit')
                return SimpleNamespace(returncode=0, stdout='{"response":"edits"}')
            raise AssertionError(cmd)
        with patch.object(agent, 'invoke', side_effect=run):
            for provider in ('claude', 'gemini'):
                code, _ = agent.call_model(provider, 'developer', 'edit', agent.WORKSPACE)
                self.assertEqual(code, 0)

    def test_structured_github_answer_can_ask_again_after_a_reply(self):
        result=agent.parse_final_message('{"factory_status":"needs_input","question":"Which Python version should we use?","summary":"Need explicit target"}', require_structured=True)
        self.assertEqual(result,('needs_input','Need explicit target','Which Python version should we use?'))
        done=agent.parse_final_message('{"factory_status":"completed","summary":"Requirement fully specified"}', require_structured=True)
        self.assertEqual(done,('ok','Requirement fully specified',None))

    def test_unstructured_github_answer_never_counts_as_success(self):
        bad=agent.parse_final_message('What framework do you want?',require_structured=True)
        self.assertEqual(bad[0],'error')
        bad=agent.parse_final_message('{"factory_status":"needs_input","question":"short"}',require_structured=True)
        self.assertEqual(bad[0],'error')
        self.assertEqual(agent.parse_final_message('Manual analysis')[0],'ok')

    def test_gemini_error_result_fails_even_when_cli_returns_zero(self):
        def fake(cmd, cwd, **kwargs):
            return SimpleNamespace(returncode=0, stdout='{"error":{"code":"expired"}}')
        with patch.object(agent, 'invoke', side_effect=fake):
            code, _ = agent.call_model('gemini', 'developer', 'test', agent.WORKSPACE)
            self.assertNotEqual(code, 0)


if __name__ == '__main__':
    unittest.main()
