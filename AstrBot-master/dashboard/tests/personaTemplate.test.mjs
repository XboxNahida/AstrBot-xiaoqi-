import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../src/components/shared/PersonaForm.vue', import.meta.url), 'utf8');

function createForm(template, ids = []) {
  const context = {
    props: { editingPersona: null, currentFolderId: 'target-folder' },
    personaForm: {}, expandedPanels: { value: [] }, smAndDown: { value: false },
    templatePersonas: { value: [template] }, selectedTemplateId: { value: template.persona_id },
    existingPersonaIds: { value: ids },
    tm: (key, args) => key === 'form.copyName' ? `${args.name}_副本` : key,
  };
  vm.createContext(context);
  for (const [start, end] of [
    ['function initializeForm(', '\nfunction closeDialog('],
    ['function applyPersonaTemplate(', '\nasync function savePersona('],
  ]) {
    const offset = source.indexOf(start);
    assert.ok(offset >= 0, `${start} must exist`);
    const limit = source.indexOf(end, offset);
    assert.ok(limit > offset);
    vm.runInContext(source.slice(offset, limit), context);
  }
  return context;
}

test('模板复制保留空助手示例及能力设置，修改副本不影响原人格', () => {
  const original = { persona_id: '小七', system_prompt: '这是完整的人格说明文本',
    custom_error_message: '稍后再试', begin_dialogs: ['哦哦', ''],
    tools: ['search'], skills: [], folder_id: 'source-folder' };
  const c = createForm(original, ['小七', '小七_副本']);
  c.applyPersonaTemplate();
  assert.equal(c.personaForm.persona_id, '小七_副本_2');
  assert.equal(c.personaForm.folder_id, 'target-folder');
  assert.equal(c.personaForm.custom_error_message, original.custom_error_message);
  assert.deepEqual(Array.from(c.personaForm.begin_dialogs), ['哦哦', '']);
  assert.deepEqual(Array.from(c.personaForm.skills), []);
  c.personaForm.begin_dialogs[0] = '变更';
  c.personaForm.tools.push('other');
  assert.deepEqual(original.begin_dialogs, ['哦哦', '']);
  assert.deepEqual(original.tools, ['search']);
});

test('默认全部能力的 null 保持不变，根目录创建不继承模板文件夹', () => {
  const c = createForm({ persona_id: '晚柠', tools: null, skills: null, folder_id: 'old' });
  c.props.currentFolderId = null;
  c.applyPersonaTemplate();
  assert.equal(c.personaForm.tools, null);
  assert.equal(c.personaForm.skills, null);
  assert.equal(c.personaForm.folder_id, null);
});

test('编辑模式和无效模板不得覆盖当前内容', () => {
  const c = createForm({ persona_id: '小七' });
  c.personaForm.system_prompt = '未保存内容';
  c.selectedTemplateId.value = '不存在';
  c.applyPersonaTemplate();
  assert.equal(c.personaForm.system_prompt, '未保存内容');
  c.selectedTemplateId.value = '小七';
  c.props.editingPersona = { persona_id: '编辑中' };
  c.applyPersonaTemplate();
  assert.equal(c.personaForm.system_prompt, '未保存内容');
});

test('选择已有人格时直接按事件ID复制完整内容，不依赖下拉框更新时序', () => {
  const original = { persona_id: '晚柠', system_prompt: '这是完整的人格说明，选择后就应显示。',
    begin_dialogs: ['你好', '在呢', '哦哦', ''], tools: null, skills: null };
  const c = createForm(original, ['晚柠']);
  c.selectedTemplateId.value = null;
  c.applyPersonaTemplate('晚柠');
  assert.equal(c.personaForm.system_prompt, original.system_prompt);
  assert.deepEqual(Array.from(c.personaForm.begin_dialogs), original.begin_dialogs);
  assert.equal(c.personaForm.persona_id, '晚柠_副本');
});
