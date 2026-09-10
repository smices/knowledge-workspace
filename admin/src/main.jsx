import React, { useEffect, useRef, useState } from 'react';
import { ProTable, StatisticCard } from '@ant-design/pro-components';
import { Alert, Avatar, Badge, Button, Card, Descriptions, Form, Input, List, Modal, Popconfirm, Select, Space, Spin, Table, Tag, Typography, Upload, message } from 'antd';
import { DatabaseOutlined, FileSearchOutlined, ReloadOutlined, UserAddOutlined, WarningOutlined } from '@ant-design/icons';

const api = async (url, options = {}) => {
  const response = await fetch(url, { credentials: 'same-origin', ...options, headers: { ...(options.headers || {}) } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败 ${response.status}`);
  return data;
};

const labels = {
  zh: { documents: '知识库文档', knowledgeBase: '知识库', chunks: 'Chunks', queryCalls: '查询调用', completed: '完成任务', inProgress: '进行中', users: '用户数', failed: '失败任务', workbench: '工作台', operations: 'OPERATIONS', overview: '知识库、检索与索引任务的实时概览。', live: '实时数据', hotQuestions: 'Top / Hot 问题', refresh: '刷新', noQueries: '暂无查询记录', notifications: '通知中心', noNotifications: '当前没有待处理通知', times: '次', selectFile: '选择文件', confirmUpload: '确认上传', confirmReplace: '确认替换', reselect: '重新选择', replaceQueued: '文档已替换并重新排队', uploadQueued: '文档已上传并排队', chooseFile: '请选择文件', replace: '替换', rebuild: '重建', rebuildAll: '重建全部索引', confirmRebuildAll: '将把当前租户所有文档重新排队，是否继续？', rebuildAllQueued: '全部文档已重新排队', delete: '删除', confirmDelete: '确认删除这份文档？', version: '版本', status: '状态', error: '错误', createdAt: '创建时间', actions: '操作', documentId: '文档 ID', indexedChunks: '已索引分段', content: '内容', document: '文档', stage: '阶段', updatedAt: '更新时间', cancelTask: '取消任务', taskCanceled: '任务已取消', action: '动作', resource: '资源', resourceId: '资源 ID', outcome: '结果', subject: '主体', time: '时间', ready: '已就绪', failedStatus: '失败', processing: '处理中', queued: '排队中', active: '活动', canceled: '已取消' },
  en: { documents: 'Documents', knowledgeBase: 'Knowledge base', chunks: 'Chunks', queryCalls: 'Query calls', completed: 'Completed tasks', inProgress: 'In progress', users: 'Users', failed: 'Failed tasks', workbench: 'Workspace', operations: 'OPERATIONS', overview: 'Live overview of knowledge, retrieval, and indexing.', live: 'Live data', hotQuestions: 'Top / Hot questions', refresh: 'Refresh', noQueries: 'No query records', notifications: 'Notifications', noNotifications: 'No pending notifications', times: 'times', selectFile: 'Choose file', confirmUpload: 'Confirm upload', confirmReplace: 'Confirm replace', reselect: 'Choose another', replaceQueued: 'Document replaced and queued', uploadQueued: 'Document uploaded and queued', chooseFile: 'Choose a file first', replace: 'Replace', rebuild: 'Rebuild', rebuildAll: 'Rebuild all indexes', confirmRebuildAll: 'Queue every document in this tenant for reindexing?', rebuildAllQueued: 'All documents queued for reindexing', delete: 'Delete', confirmDelete: 'Delete this document?', version: 'Version', status: 'Status', error: 'Error', createdAt: 'Created', actions: 'Actions', documentId: 'Document ID', indexedChunks: 'Indexed chunks', content: 'Content', document: 'Document', stage: 'Stage', updatedAt: 'Updated', cancelTask: 'Cancel task', taskCanceled: 'Task canceled', action: 'Action', resource: 'Resource', resourceId: 'Resource ID', outcome: 'Outcome', subject: 'Subject', time: 'Time', ready: 'Ready', failedStatus: 'Failed', processing: 'Processing', queued: 'Queued', active: 'Active', canceled: 'Canceled' },
};
const languageText = (t, zh, en) => t === labels.zh ? zh : en;

function useAdminLanguage() {
  const [language, setLanguage] = useState(() => window.localStorage.getItem('sn-admin-language') || 'zh');
  useEffect(() => { const onLanguage = (event) => setLanguage(event.detail === 'en' ? 'en' : 'zh'); window.addEventListener('sn-admin-language', onLanguage); return () => window.removeEventListener('sn-admin-language', onLanguage); }, []);
  return labels[language];
}

function StatusTag({ value, t }) {
  const color = { ready: 'success', failed: 'error', processing: 'processing', queued: 'warning', active: 'default' }[value] || 'default';
  return <Tag color={color}>{t?.[value === 'failed' ? 'failedStatus' : value] || value}</Tag>;
}

function Dashboard() {
  const t = useAdminLanguage();
  const [data, setData] = useState({ documents: 0, chunks: 0, query_calls: 0, completed_tasks: 0, in_progress_tasks: 0, users: 0, failed_tasks: 0 });
  const [questions, setQuestions] = useState([]);
  const [notifications, setNotifications] = useState([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const load = () => Promise.all([api('/api/v1/admin/overview'), api('/api/v1/admin/questions'), api('/api/v1/admin/notifications')])
    .then(([overview, questionData, notificationData]) => { setData(overview); setQuestions(questionData.items || []); setNotifications(notificationData.items || []); setError(''); })
    .catch((e) => setError(e.message))
    .finally(() => setLoading(false));
  useEffect(() => { load(); }, []);
  const cards = [
    [t.documents, data.documents, <DatabaseOutlined />], [t.chunks, data.chunks, <FileSearchOutlined />],
    [t.queryCalls, data.query_calls, <FileSearchOutlined />], [t.completed, data.completed_tasks, <ReloadOutlined />],
    [t.inProgress, data.in_progress_tasks, <ReloadOutlined />], [t.users, data.users, <Avatar size="small" />], [t.failed, data.failed_tasks, <WarningOutlined />],
  ];
  return <Spin spinning={loading}>
    <Space direction="vertical" size={20} style={{ width: '100%' }}>
      <div className="dashboard-heading"><div><p className="dashboard-kicker">{t.operations}</p><h1>{t.workbench}</h1><p>{t.overview}</p></div><Tag color="blue">{t.live}</Tag></div>
      {error && <Alert type="error" message={error} showIcon />}
      <div className="stat-grid">{cards.map(([title, value, icon]) => <StatisticCard className="metric-card" key={title} statistic={{ title, value, icon }} />)}</div>
      <div className="dashboard-grid">
        <Card className="dashboard-card" title={t.hotQuestions} extra={<Button icon={<ReloadOutlined />} onClick={() => { setLoading(true); load(); }}>{t.refresh}</Button>}>
          <List size="small" dataSource={questions.slice(0, 6)} locale={{ emptyText: t.noQueries }} renderItem={(item, index) => <List.Item><span className="question-row"><b>#{index + 1}</b><span>{item.question}</span></span><Tag>{item.count} {t.times}</Tag></List.Item>} />
      </Card>
        <Card className="dashboard-card notification-card" title={<span>{t.notifications} <Badge count={notifications.length} offset={[8, -2]} /></span>}>
        <List size="small" dataSource={notifications} locale={{ emptyText: <div className="empty-panel">{t.noNotifications}</div> }} renderItem={(item) => <List.Item><List.Item.Meta avatar={<WarningOutlined style={{ color: '#d4380d' }} />} title={item.title} description={item.description} /></List.Item>} />
      </Card>
      </div>
    </Space>
  </Spin>;
}

function Documents() {
  const t = useAdminLanguage();
  const [feedback, contextHolder] = message.useMessage();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const pending = useRef(null);
  const [file, setFile] = useState(null);
  const [replaceTarget, setReplaceTarget] = useState(null);
  const [preview, setPreview] = useState(null);
  const load = async () => {
    pending.current?.abort();
    const controller = new AbortController(); pending.current = controller;
    setLoading(true);
    try {
      const data = await api(`/api/v1/admin/documents?offset=${(page - 1) * 20}&limit=20`, { signal: controller.signal });
      if (controller.signal.aborted) return;
      setRows(data.items); setTotal(data.total);
      if (page > 1 && !data.items.length) setPage(Math.max(1, Math.ceil(data.total / 20)));
    } catch (e) { if (e.name !== 'AbortError') feedback.error(e.message); }
    finally { if (!controller.signal.aborted) setLoading(false); }
  };
  useEffect(() => { load(); return () => pending.current?.abort(); }, [page]);
  const selectFile = (next, target = null) => { setFile(next); setReplaceTarget(target); return false; };
  const clearFile = () => { setFile(null); setReplaceTarget(null); };
  const sendFile = async () => {
    if (!file) return feedback.warning(t.chooseFile);
    const method = replaceTarget ? 'PUT' : 'POST';
    const url = replaceTarget ? `/api/v1/documents/${replaceTarget.id}` : '/api/v1/documents';
    try {
      const body = new FormData(); body.append('file', file);
      await api(url, { method, body });
      feedback.success(method === 'PUT' ? t.replaceQueued : t.uploadQueued);
      clearFile(); await load();
    } catch (e) { feedback.error(e.message); }
  };
  const columns = [
    { title: t.document, dataIndex: 'title', ellipsis: true, render: (v, row) => <Button type="link" onClick={async () => { try { setPreview(await api(`/api/v1/documents/${row.id}/content`)); } catch (e) { feedback.error(e.message); } }}>{v}</Button> },
    { title: t.knowledgeBase, dataIndex: 'knowledge_base', ellipsis: true, render: (v) => v || '—' },
    { title: t.version, dataIndex: 'version', render: (v) => v || 1 },
    { title: t.status, dataIndex: 'status', render: (v) => <StatusTag value={v} t={t} /> },
    { title: t.error, dataIndex: 'error', ellipsis: true, render: (v) => v ? <Typography.Text type="danger">{v}</Typography.Text> : '—' },
    { title: t.createdAt, dataIndex: 'created_at', valueType: 'dateTime' },
    { title: t.actions, render: (_, row) => <Space><Upload showUploadList={false} beforeUpload={(next) => selectFile(next, row)}><Button type="link">{t.replace}</Button></Upload>{row.status === 'failed' && <Button type="link" onClick={async () => { await api(`/api/v1/documents/${row.id}/reindex`, { method: 'POST' }); message.success(t.queued); load(); }}>{t.rebuild}</Button>}<Popconfirm title={t.confirmDelete} onConfirm={async () => { await api(`/api/v1/documents/${row.id}`, { method: 'DELETE' }); message.success(t.delete); load(); }}><Button danger type="link">{t.delete}</Button></Popconfirm></Space> },
  ];
  return <Card title={t.documents} extra={<Space><Popconfirm title={t.confirmRebuildAll} onConfirm={async () => { await api('/api/v1/admin/documents/reindex-all', { method: 'POST' }); message.success(t.rebuildAllQueued); load(); }}><Button>{t.rebuildAll}</Button></Popconfirm><Upload showUploadList={false} beforeUpload={(next) => selectFile(next)}><Button type="primary">{t.selectFile}</Button></Upload></Space>}>
    {file && <div className="pending-upload"><span>{languageText(t, '已选择：', 'Selected: ')}<b>{file.name}</b>{replaceTarget ? languageText(t, `，将替换「${replaceTarget.title}」`, `, replacing “${replaceTarget.title}”`) : languageText(t, '，将作为新文档上传', ', ready to upload')}</span><Space><Button type="primary" onClick={sendFile}>{replaceTarget ? t.confirmReplace : t.confirmUpload}</Button><Button onClick={clearFile}>{t.reselect}</Button></Space></div>}
    {contextHolder}
    <Table rowKey="id" loading={loading} dataSource={rows} columns={columns} scroll={{ x: 1000 }} pagination={{ current: page, pageSize: 20, total, showSizeChanger: false, onChange: setPage }} />
    <Modal open={Boolean(preview)} title={preview?.title} width={900} footer={null} onCancel={() => setPreview(null)}><Descriptions bordered column={1}><Descriptions.Item label={t.documentId}>{preview?.document_id}</Descriptions.Item><Descriptions.Item label={t.indexedChunks}>{preview?.chunk_count ?? 0}</Descriptions.Item><Descriptions.Item label={t.content}><pre className="content-preview">{preview?.content}</pre></Descriptions.Item></Descriptions></Modal>
  </Card>;
}

function Tasks() {
  const t = useAdminLanguage();
  const columns = [
    { title: t.document, dataIndex: 'title' },
    { title: t.stage, dataIndex: 'stage' },
    { title: t.status, dataIndex: 'status', render: (_, row) => <StatusTag value={row.status} t={t} /> },
    { title: t.error, dataIndex: 'error', ellipsis: true, render: (v) => v || '—' },
    { title: t.updatedAt, dataIndex: 'updated_at', valueType: 'dateTime' },
    { title: t.actions, valueType: 'option', render: (_, row) => !['ready', 'failed', 'canceled'].includes(row.status) ? <Button danger type="link" onClick={async () => { await api(`/api/v1/documents/${row.id}/cancel`, { method: 'POST' }); message.success(t.taskCanceled); window.location.reload(); }}>{t.cancelTask}</Button> : null },
  ];
  return <ProTable rowKey="id" request={async () => ({ data: (await api('/api/v1/admin/tasks')).items, success: true })} columns={columns} search={false} pagination={{ pageSize: 20 }} />;
}

function Logs() {
  const t = useAdminLanguage();
  const columns = [
    { title: t.action, dataIndex: 'action' },
    { title: t.resource, dataIndex: 'resource_type' },
    { title: t.resourceId, dataIndex: 'resource_id', ellipsis: true },
    { title: t.outcome, dataIndex: 'outcome' },
    { title: t.subject, dataIndex: 'subject' },
    { title: t.time, dataIndex: 'created_at', valueType: 'dateTime' },
  ];
  return <ProTable rowKey="id" request={async () => ({ data: (await api('/api/v1/admin/logs')).items, success: true })} columns={columns} search={false} pagination={{ pageSize: 20 }} />;
}

function Members() {
  const t = useAdminLanguage();
  const [feedback, contextHolder] = message.useMessage();
  const [data, setData] = useState({ items: [], roles: [] });
  const [editing, setEditing] = useState(null);
  const [roles, setRoles] = useState([]);
  const [loading, setLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [createSource, setCreateSource] = useState('local');
  const [createSubmitting, setCreateSubmitting] = useState(false);
  const [resetting, setResetting] = useState(null);
  const [resetSubmitting, setResetSubmitting] = useState(false);
  const [createForm] = Form.useForm();
  const [resetForm] = Form.useForm();
  const usernamePattern = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
  const rolePattern = /^[A-Za-z0-9:_-]{1,128}$/;
  const load = async () => { setLoading(true); try { setData(await api('/api/v1/admin/members')); } catch (e) { feedback.error(e.message); } finally { setLoading(false); } };
  useEffect(() => { load(); }, []);
  const closeCreate = () => { setCreateOpen(false); createForm.resetFields(); setCreateSource('local'); };
  const closeReset = () => { setResetting(null); resetForm.resetFields(); };
  const changeSource = (source) => { setCreateSource(source); createForm.resetFields(['username', 'password', 'subject', 'roles']); };
  const roleValues = (values) => {
    const next = [...new Set((values || []).map((value) => String(value).trim()).filter(Boolean))];
    if (next.some((value) => !rolePattern.test(value))) throw new Error(languageText(t, '角色只能包含字母、数字、冒号、下划线或连字符。', 'Roles may contain letters, numbers, colon, underscore, and hyphen only.'));
    return next;
  };
  const saveRoles = async () => {
    try {
      const nextRoles = roleValues(roles);
      await api(`/api/v1/admin/members/${encodeURIComponent(editing.subject)}/roles`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ roles: nextRoles }) });
      feedback.success(languageText(t, '权限已更新', 'Access updated')); setEditing(null); load();
    } catch (e) { feedback.error(e.message); }
  };
  const setStatus = async (row, status) => { try { await api(`/api/v1/admin/members/${encodeURIComponent(row.subject)}/status`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status }) }); feedback.success(languageText(t, '状态已更新', 'Status updated')); load(); } catch (e) { feedback.error(e.message); } };
  const createMember = async () => {
    try {
      const values = await createForm.validateFields();
      const nextRoles = roleValues(values.roles);
      const payload = createSource === 'local'
        ? { source: 'local', username: values.username.trim(), password: values.password, roles: nextRoles }
        : { source: 'idp', subject: values.subject, roles: nextRoles };
      setCreateSubmitting(true);
      await api('/api/v1/admin/members', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      feedback.success(languageText(t, '成员已创建', 'Member created'));
      closeCreate();
      load();
    } catch (e) {
      if (!e?.errorFields) feedback.error(e.message);
    } finally { setCreateSubmitting(false); }
  };
  const resetPassword = async () => {
    try {
      const values = await resetForm.validateFields();
      setResetSubmitting(true);
      await api(`/api/v1/admin/members/${encodeURIComponent(resetting.subject)}/password`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ new_password: values.new_password }) });
      feedback.success(languageText(t, '密码已重置', 'Password reset'));
      closeReset();
    } catch (e) {
      if (!e?.errorFields) feedback.error(e.message);
    } finally { setResetSubmitting(false); }
  };
  const sourceLabel = (row) => row.source === 'local'
    ? (row.initial_local_admin ? languageText(t, '初始化管理员', 'Initial admin') : languageText(t, '本地用户', 'Local user'))
    : row.source === 'service' ? languageText(t, '机器账号', 'Service') : 'IdP';
  const roleRules = [{ validator: (_, value) => { try { roleValues(value); return Promise.resolve(); } catch (error) { return Promise.reject(error); } } }];
  const columns = [
    { title: languageText(t, '用户名', 'Username'), dataIndex: 'username', render: (_, row) => <div><b>{row.username || row.subject}</b>{row.display_name && <div><Typography.Text type="secondary">{row.display_name}</Typography.Text></div>}</div>, ellipsis: true },
    { title: languageText(t, '账号标识', 'Subject'), dataIndex: 'subject', ellipsis: true },
    { title: languageText(t, '来源', 'Source'), dataIndex: 'source', render: (_, row) => <Tag color={row.source === 'local' ? 'gold' : row.source === 'service' ? 'purple' : 'blue'}>{sourceLabel(row)}</Tag> },
    { title: languageText(t, '应用角色', 'App roles'), dataIndex: 'roles', render: (value) => value?.length ? value.map((role) => <Tag key={role}>{role}</Tag>) : '—' },
    { title: languageText(t, '状态', 'Status'), dataIndex: 'status', render: (value) => <StatusTag value={value} t={t} /> },
    { title: t.actions, render: (_, row) => row.initial_local_admin ? <Typography.Text type="secondary">{languageText(t, '仅本人可改密', 'Self-service only')}</Typography.Text> : row.source === 'service' ? <Typography.Text type="secondary">—</Typography.Text> : <Space wrap><Button type="link" onClick={() => { setEditing(row); setRoles(row.roles || []); }}>{languageText(t, '配置权限', 'Access')}</Button>{row.source === 'local' && <Button type="link" onClick={() => { setResetting(row); resetForm.resetFields(); }}>{languageText(t, '重置密码', 'Reset password')}</Button>}<Popconfirm title={row.status === 'active' ? languageText(t, '禁用后该账号将无法继续访问本应用，是否继续？', 'Disable this account from this application?') : languageText(t, '重新启用该账号？', 'Enable this account?')} onConfirm={() => setStatus(row, row.status === 'active' ? 'disabled' : 'active')}><Button type="link" danger={row.status === 'active'}>{row.status === 'active' ? languageText(t, '禁用', 'Disable') : languageText(t, '启用', 'Enable')}</Button></Popconfirm></Space> },
  ];
  return <Card title={languageText(t, '成员与权限', 'Members & access')} extra={<Button type="primary" icon={<UserAddOutlined />} onClick={() => { createForm.resetFields(); setCreateSource('local'); setCreateOpen(true); }}>{languageText(t, '添加成员', 'Add member')}</Button>}>
    {contextHolder}
    <Alert showIcon type="info" message={languageText(t, '可预准入企业身份账号，或创建普通本地用户。初始管理员不可由其他管理员重置、禁用或改权限。', 'Pre-admit IdP identities or create ordinary local users. The initial administrator cannot be reset, disabled, or permission-edited by another admin.')} style={{ marginBottom: 16 }} />
    <Table rowKey="subject" loading={loading} dataSource={data.items} columns={columns} pagination={{ pageSize: 20 }} />
    <Modal title={languageText(t, '配置应用角色', 'Configure application roles')} open={Boolean(editing)} onCancel={() => setEditing(null)} onOk={saveRoles} okText={languageText(t, '保存', 'Save')}><p>{editing?.subject}</p><Select mode="tags" style={{ width: '100%' }} value={roles} onChange={setRoles} options={(data.roles || []).map((role) => ({ value: role }))} tokenSeparators={[',']} placeholder={languageText(t, '输入或选择角色', 'Choose or enter roles')} /></Modal>
    <Modal title={languageText(t, '添加成员', 'Add member')} open={createOpen} onCancel={() => { if (!createSubmitting) closeCreate(); }} onOk={createMember} confirmLoading={createSubmitting} destroyOnClose okText={languageText(t, '创建', 'Create')}>
      <Form form={createForm} layout="vertical" initialValues={{ roles: [] }}>
        <Form.Item label={languageText(t, '身份来源', 'Identity source')}><Select value={createSource} onChange={changeSource} options={[{ value: 'local', label: languageText(t, '本地用户', 'Local user') }, { value: 'idp', label: 'IdP' }]} /></Form.Item>
        {createSource === 'local' ? <><Form.Item name="username" normalize={(value) => value?.trim()} label={languageText(t, '用户名', 'Username')} rules={[{ required: true, message: languageText(t, '请输入用户名', 'Enter a username') }, { pattern: usernamePattern, message: languageText(t, '使用 1–64 位 ASCII 字母、数字、点、下划线或连字符，且首位必须是字母或数字', 'Use 1–64 ASCII letters, numbers, dot, underscore, or hyphen; the first character must be a letter or number') }]}><Input autoComplete="off" minLength={1} maxLength={64} /></Form.Item><Form.Item name="password" label={languageText(t, '初始密码', 'Initial password')} rules={[{ required: true, message: languageText(t, '请输入密码', 'Enter a password') }, { min: 16, max: 128, message: languageText(t, '密码长度必须为 16–128 个字符', 'Password must be 16–128 characters') }]}><Input.Password autoComplete="new-password" /></Form.Item></> : <Form.Item name="subject" label={languageText(t, 'IdP 稳定 subject', 'Stable IdP subject')} rules={[{ required: true, message: languageText(t, '请输入 subject', 'Enter the stable subject') }, { max: 256, message: languageText(t, 'subject 不能超过 256 个字符', 'Subject cannot exceed 256 characters') }]}><Input autoComplete="off" /></Form.Item>}
        <Form.Item name="roles" label={languageText(t, '应用角色', 'Application roles')} rules={roleRules}><Select mode="tags" tokenSeparators={[',']} options={(data.roles || []).map((role) => ({ value: role }))} placeholder={languageText(t, '选择或输入角色', 'Choose or enter roles')} /></Form.Item>
      </Form>
    </Modal>
    <Modal title={languageText(t, '重置本地用户密码', 'Reset local user password')} open={Boolean(resetting)} onCancel={() => { if (!resetSubmitting) closeReset(); }} onOk={resetPassword} confirmLoading={resetSubmitting} destroyOnClose okText={languageText(t, '重置', 'Reset')}>
      <Form form={resetForm} layout="vertical"><Form.Item name="new_password" label={languageText(t, '新密码', 'New password')} rules={[{ required: true, message: languageText(t, '请输入新密码', 'Enter a new password') }, { min: 16, max: 128, message: languageText(t, '密码长度必须为 16–128 个字符', 'Password must be 16–128 characters') }]}><Input.Password autoComplete="new-password" /></Form.Item></Form>
    </Modal>
  </Card>;
}

function Relations() {
  const t = useAdminLanguage();
  const columns = [
    { title: languageText(t, '实体', 'Source'), dataIndex: 'source' },
    { title: languageText(t, '关系', 'Relation'), dataIndex: 'relation', render: (value) => <Tag color="purple">{value}</Tag> },
    { title: languageText(t, '实体', 'Target'), dataIndex: 'target' },
    { title: t.document, dataIndex: 'document', ellipsis: true },
    { title: t.version, dataIndex: 'document_version' },
    { title: languageText(t, '证据片段', 'Evidence'), dataIndex: 'excerpt', ellipsis: true },
  ];
  return <ProTable rowKey="id" headerTitle={languageText(t, '关系证据', 'Relation evidence')} request={async () => ({ data: (await api('/api/v1/admin/relations')).items, success: true })} columns={columns} search={false} />;
}

function EntityAliases() {
  const t = useAdminLanguage();
  const [items, setItems] = useState([]); const [draft, setDraft] = useState(null);
  const load = async () => { try { setItems((await api('/api/v1/admin/entity-aliases')).items); } catch (e) { message.error(e.message); } };
  useEffect(() => { load(); }, []);
  const update = async (id, status) => { try { await api(`/api/v1/admin/entity-aliases/${id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status }) }); message.success(languageText(t, '称谓状态已更新', 'Alias updated')); load(); } catch (e) { message.error(e.message); } };
  const create = async () => { try { await api('/api/v1/admin/entity-aliases', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...draft, chunk_index: Number(draft.chunk_index) }) }); message.success(languageText(t, '候选称谓已创建', 'Alias candidate created')); setDraft(null); load(); } catch (e) { message.error(e.message); } };
  const columns = [
    { title: languageText(t, '标准实体', 'Canonical entity'), dataIndex: 'canonical' }, { title: languageText(t, '原文称谓', 'Source mention'), dataIndex: 'alias' },
    { title: t.document, dataIndex: 'document', ellipsis: true }, { title: t.version, dataIndex: 'document_version' }, { title: languageText(t, '证据块', 'Evidence chunk'), dataIndex: 'chunk_index' },
    { title: t.status, dataIndex: 'status', render: (value) => <Tag color={value === 'approved' ? 'green' : value === 'rejected' ? 'red' : 'gold'}>{value}</Tag> },
    { title: languageText(t, '证据片段', 'Evidence'), dataIndex: 'excerpt', ellipsis: true },
    { title: t.actions, render: (_, row) => row.status === 'candidate' ? <Space><Button type="link" onClick={() => update(row.id, 'approved')}>{languageText(t, '批准', 'Approve')}</Button><Button type="link" danger onClick={() => update(row.id, 'rejected')}>{languageText(t, '拒绝', 'Reject')}</Button></Space> : '—' },
  ];
  return <Card title={languageText(t, '实体称谓审核', 'Entity alias review')} extra={<Button type="primary" onClick={() => setDraft({ document_id: '', chunk_index: 0, canonical: '', alias: '' })}>{languageText(t, '新建候选', 'New candidate')}</Button>}><Alert showIcon type="info" message={languageText(t, '只有已批准、且在同一原文块共同出现的称谓映射才参与检索；文档重建或替换会使其失效。', 'Only approved mappings with same-chunk evidence participate in retrieval. Reindexing or replacement invalidates them.')} style={{ marginBottom: 16 }} /><Table rowKey="id" dataSource={items} columns={columns} pagination={{ pageSize: 20 }} /><Modal title={languageText(t, '新建称谓候选', 'New alias candidate')} open={Boolean(draft)} onCancel={() => setDraft(null)} onOk={create}><Space direction="vertical" style={{ width: '100%' }}><Input placeholder={languageText(t, '文档 ID', 'Document ID')} value={draft?.document_id} onChange={(e) => setDraft({ ...draft, document_id: e.target.value })} /><Input placeholder={languageText(t, '证据块序号', 'Evidence chunk index')} value={draft?.chunk_index} onChange={(e) => setDraft({ ...draft, chunk_index: e.target.value })} /><Input placeholder={languageText(t, '标准实体', 'Canonical entity')} value={draft?.canonical} onChange={(e) => setDraft({ ...draft, canonical: e.target.value })} /><Input placeholder={languageText(t, '原文称谓', 'Source mention')} value={draft?.alias} onChange={(e) => setDraft({ ...draft, alias: e.target.value })} /></Space></Modal></Card>;
}

export { Dashboard, Documents, Tasks, Logs, Members, Relations, EntityAliases };
