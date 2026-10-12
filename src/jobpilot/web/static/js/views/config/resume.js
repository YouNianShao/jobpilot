/* 配置页的「简历正文」分组：上传区、编辑框、导入和保存。
   它有自己的保存按钮和接口，和配置表单的「保存配置」是两回事。
   保存后要重拉配置、刷新状态：这两个动作由 index.js 调用时传进来（不反过来 import index.js，免得循环依赖）。 */

import { post, upload } from '../../api.js';
import { $, state } from '../../store.js';
import { alertDialog, el, icon, toast, withLoading } from '../../ui.js';

const RESUME_ACCEPT = '.docx,.pdf,.md,.markdown,.txt';

/* 简历正文：直接在这里编辑 / 从本地文件导入。
   评分和招呼语都吃这份正文，读不到时后端会直接拒绝干活，所以状态要一眼可见。 */
export function resumeGroup({ loadConfig, refresh }) {
  const info = state.resume || {};
  const group = el('div', { class: 'fgroup' });
  group.appendChild(el('div', { class: 'fgroup-head' }, [
    icon('user'),
    el('h3', { text: '简历正文' }),
    el('div', { class: 'grow' }),
    el('span', { class: 'pill ' + (info.ok ? 'ok' : 'bad'), text: info.ok ? ('已加载 ' + info.chars + ' 字') : '读不到' }),
  ]));

  const body = el('div', { class: 'fgroup-body' });
  body.appendChild(el('div', {
    class: 'fgroup-note',
    text: info.ok
      ? ('当前文件：' + (info.path || '（未配置）') + '。AI 评分、招呼语、定制简历都用这份正文。')
      : ('⚠ ' + (info.problem || '还没有配置简历') + '　—— 读不到简历时，评分和招呼语会直接拒绝执行，而不是拿空简历硬算。'),
  }));

  // 上传区：Word / PDF / Markdown / 纯文本都行，支持拖拽。
  // 文件只在服务端抽取正文、不落盘：抽出来先进下面的编辑框给你过目（PDF 的换行常常不理想），点保存才生效。
  const fileInput = el('input', { type: 'file', accept: RESUME_ACCEPT, hidden: true });
  const dropText = el('span', { text: '支持 Word（.docx）、PDF（需带文字层，扫描件不行）、Markdown、纯文本，最大 10 MB' });
  const drop = el('div', {
    class: 'dropzone', role: 'button', tabindex: '0', 'aria-label': '上传简历文件',
    onclick: () => fileInput.click(),
    onkeydown: (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); fileInput.click(); } },
  }, [
    el('div', { class: 'dropzone-ico' }, [icon('upload')]),
    el('div', { class: 'dropzone-text' }, [el('b', { text: '点击选择或把简历文件拖到这里' }), dropText]),
    el('div', { class: 'fmt-tags' }, ['DOCX', 'PDF', 'MD', 'TXT'].map((t) => el('span', { class: 'fmt-tag', text: t }))),
  ]);
  body.appendChild(drop);

  const editor = el('textarea', { class: 'resume-editor', spellcheck: 'false', id: 'resumeEditor' });
  editor.value = info.text || '';
  editor.rows = 14;
  const counter = el('span', { text: (editor.value.trim().length) + ' 字' });
  const editorField = el('label', { class: 'field wide' }, [
    el('span', { class: 'field-meta' }, [el('span', { class: 'label', text: '正文（导入后可以直接在这里改）' }), counter]),
    editor,
  ]);
  body.appendChild(editorField);
  const checkLength = () => {
    const n = editor.value.trim().length;
    counter.textContent = n + ' 字' + (n && n < 50 ? ' · 至少 50 字' : '');
    counter.parentElement.classList.toggle('bad', !!n && n < 50);
  };
  editor.addEventListener('input', checkLength);
  checkLength();

  const importFile = async (file) => {
    if (!file) return;
    const ext = (file.name.match(/\.[^.]+$/) || [''])[0].toLowerCase();
    if (!RESUME_ACCEPT.split(',').includes(ext) && ext !== '.doc') {
      await alertDialog('格式不支持', '「' + file.name + '」不是支持的格式。\n请上传 Word（.docx）、PDF、Markdown 或纯文本。', 'warn');
      return;
    }
    if (file.size > 10 * 1024 * 1024) { await alertDialog('文件太大', '简历文件最大 10 MB。', 'warn'); return; }
    drop.classList.add('is-busy');
    dropText.textContent = '正在读取 ' + file.name + '…';
    const res = await upload('/api/resume/import?filename=' + encodeURIComponent(file.name), file);
    drop.classList.remove('is-busy');
    dropText.textContent = '支持 Word（.docx）、PDF（需带文字层，扫描件不行）、Markdown、纯文本，最大 10 MB';
    if (res.error) { await alertDialog('简历读不出来', res.error); return; }
    editor.value = res.text;
    checkLength();
    editor.dispatchEvent(new Event('input', { bubbles: true }));
    toast('已从 ' + res.format.toUpperCase() + ' 读入 ' + res.chars + ' 字，核对后点「保存简历」（不想要就点「恢复成已保存的内容」）', 'ok');
    editor.focus();
  };
  fileInput.addEventListener('change', () => { importFile(fileInput.files && fileInput.files[0]); fileInput.value = ''; });
  for (const name of ['dragenter', 'dragover']) {
    drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.add('is-over'); });
  }
  for (const name of ['dragleave', 'drop']) {
    drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.remove('is-over'); });
  }
  drop.addEventListener('drop', (event) => importFile(event.dataTransfer && event.dataTransfer.files[0]));

  const saveBtn = el('button', {
    class: 'btn primary small', text: '保存简历',
    onclick: () => withLoading(saveBtn, async () => {
      if (editor.value.trim().length < 50) { await alertDialog('简历太短了', '至少需要 50 字，先导入文件或把正文粘贴进来。', 'warn'); return; }
      const res = await post('/api/resume', { text: editor.value });
      if (res.error) { await alertDialog('保存失败', res.error); return; }
      toast('简历已保存（' + res.chars + ' 字）', 'ok');
      await loadConfig();      // 路径可能变了，重新拉配置
      await refresh();
    }),
  });
  body.appendChild(el('div', { class: 'actions' }, [
    saveBtn,
    el('button', {
      class: 'btn ghost small', text: '恢复成已保存的内容',
      onclick: () => { editor.value = (state.resume && state.resume.text) || ''; checkLength(); },
    }),
    fileInput,
  ]));
  group.appendChild(body);
  return group;
}
