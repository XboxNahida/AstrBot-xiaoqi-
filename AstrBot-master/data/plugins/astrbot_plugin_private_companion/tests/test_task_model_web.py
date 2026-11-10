"""网页任务预算保存和实际SDK请求级超时的合成验证。"""

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from astrbot_plugin_private_companion.page_api import PrivateCompanionPageApi


def test_task_model_web_defaults_roundtrip_and_disable_fallback():
    page = PrivateCompanionPageApi.__new__(PrivateCompanionPageApi)
    page.plugin = SimpleNamespace(
        config={}, conversation_task_fallback_provider_id="legacy"
    )
    page._schema_key_index_cache = None
    for key, value in (
        ("task_model_timeout_seconds", 60),
        ("task_model_max_attempts", 3),
    ):
        assert key in page._allowed_setting_keys()
        normalized = page._normalize_setting_value(key, value)
        page._set_config_value(key, normalized)
        page._apply_config_value(key, normalized)
        assert getattr(page.plugin, key) == value
        assert page._config_get_raw(key) == value
    page._apply_config_value("CONVERSATION_TASK_FALLBACK_PROVIDER_ID", "legacy")
    assert page.plugin.conversation_task_fallback_provider_id == ""


def test_task_model_panel_collects_total_attempts_and_timeout():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node不可用")
    panel = (
        Path(__file__).resolve().parents[1]
        / "pages/companion-panel/js/panels/provider-tree.js"
    )
    source = (
        "global.window={};\neval("
        + json.dumps(panel.read_text(encoding="utf-8"))
        + ");\n"
        + """
const inputs = {'[data-task-model-timeout]': {value:'180'}, '[data-task-model-attempts]':{value:'3'}};
const context = {state:{overview:{settings:{task_model_timeout_seconds:120,task_model_max_attempts:3}}},
 document:{querySelector:(key)=>inputs[key]}};
const result=window.PrivateCompanionProviderTree.currentTaskModelValues(context);
if(result.task_model_timeout_seconds!==60 || result.task_model_max_attempts!==3) throw Error('wrong budget');
inputs['[data-task-model-attempts]'].value='0';
let rejected=false;
try {window.PrivateCompanionProviderTree.currentTaskModelValues(context);} catch(e){rejected=true;}
if(!rejected)throw Error('invalid attempts accepted');
"""
    )
    result = subprocess.run(
        [node, "-"], input=source, capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("page_name", ["companion-panel", "陪伴面板"])
def test_codex_panel_actual_save_handler_and_reload(page_name):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable")
    folder = Path(__file__).resolve().parents[1] / "pages" / page_name
    app = (folder / "app.js").read_text(encoding="utf8")
    handler = app.split(
        '$("#saveProvidersBtn").addEventListener("click", async () => {', 1
    )[1].split("\n});", 1)[0]
    script = (
        "global.window={};\neval("
        + json.dumps((folder / "js/panels/provider-tree.js").read_text(encoding="utf8"))
        + ");\n"
        + """
const inputs = {'[data-task-execution-engine]':{value:'native'}, '[data-codex-reasoning-effort]':{value:'xhigh'}};
const document = {querySelector:key=>inputs[key]};
const state = {overview:{settings:{task_execution_engine:'codex',codex_reasoning_effort:'medium'}}};
let saved;
const currentProviderValues=()=>({}), currentProviderTimeoutValues=()=>({});
const currentProviderTokenLimitValues=()=>({}),currentProviderFallbackValues=()=>({});
const currentProviderConfigMode=()=> 'quick', providerLabels={};
const visibleConfigKey=()=>true,providerAllowedInCurrentMode=()=>true;
const showToast=(m)=>{throw Error(m)}, $=()=>({}),renderProviders=()=>{};
const runAction=async fn=>fn(), postJson=async(path,payload)=>{saved=payload;return {success:true}}, actionResultPersisted=()=>true;
window.PrivateCompanionProviderTree.currentDeepseekPeakValues=()=>({});
async function save(){
"""
        + handler
        + """
}
(async()=>{
await save();
if(saved.settings.task_execution_engine!=='native'||saved.settings.codex_reasoning_effort!=='xhigh')throw Error('save omitted values');
if(state.taskModelDraft.codex_reasoning_effort!=='xhigh')throw Error('draft not retained');
const reloaded={document:{querySelector:()=>null},state:{overview:{settings:saved.settings}}};
const values=window.PrivateCompanionProviderTree.currentTaskModelValues(reloaded);
if(values.task_execution_engine!=='native'||values.codex_reasoning_effort!=='xhigh')throw Error('reload lost server values');
inputs['[data-codex-reasoning-effort]'].value='bad\\n';
let invalid=false;try{window.PrivateCompanionProviderTree.currentTaskModelValues({document,state})}catch(e){invalid=true}
if(!invalid)throw Error('control character accepted');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    )
    result = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, encoding="utf8"
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_task_sdk_timeout_overrides_provider_35_seconds_without_shared_mutation(
    monkeypatch,
):
    observed = []

    async def transport(request):
        observed.append(request.extensions["timeout"]["read"])
        assert json.loads(request.content)["stream"] is True
        chunk = {
            "id": "test",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "gpt-test",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(
        ProviderOpenAIOfficial, "_create_http_client", lambda *_: client
    )
    provider = ProviderOpenAIOfficial(
        {
            "id": "test",
            "type": "openai_chat_completion",
            "model": "gpt-test",
            "key": ["fake-key"],
            "api_base": "https://test.example/v1",
            "timeout": 35,
        },
        {},
    )
    try:
        await asyncio.gather(
            *(
                provider.text_chat(
                    prompt="test",
                    request_max_retries=0,
                    **({"timeout": timeout} if timeout else {}),
                )
                for timeout in (120, 240, 10, None)
            )
        )
        assert sorted(observed) == [10, 35, 60, 60]
        assert provider.timeout == 35 and provider.client.timeout == 35
    finally:
        await provider.client.close()
