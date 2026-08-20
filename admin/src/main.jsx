import React, { useEffect, useState } from 'react';
import { ProTable, StatisticCard } from '@ant-design/pro-components';
import { Alert, Avatar, Badge, Button, Card, Descriptions, List, Modal, Popconfirm, Space, Spin, Table, Tag, Typography, Upload, message } from 'antd';
import { DatabaseOutlined, FileSearchOutlined, ReloadOutlined, WarningOutlined } from '@ant-design/icons';

const api = async (url, options = {}) => {
  const response = await fetch(url, { ...options, headers: { ...(options.headers || {}) } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败 ${response.status}`);
  return data;
};

const labels = {
  zh: { documents: '知识库文档', knowledgeBase: '知识库', chunks: 'Chunks', queryCalls: '查询调用', completed: '完成任务', inProgress: '进行中', users: '用户数', failed: '失败任务', workbench: '工作台', operations: 'OPERATIONS', overview: '知识库、检索与索引任务的实时概览。', live: '实时数据', hotQuestions: 'Top / Hot 问题', refresh: '刷新', noQueries: '暂无查询记录', notifications: '通知中心', noNotifications: '当前没有待处理通知', times: '次', selectFile: '选择文件', confirmUpload: '确认上传', confirmReplace: '确认替换', reselect: '重新选择', replaceQueued: '文档已替换并重新排队', uploadQueued: '文档已上传并排队', chooseFile: '请选择文件', replace: '替换', rebuild: '重建', delete: '删除', confirmDelete: '确认删除这份文档？', version: '版本', status: '状态', error: '错误', createdAt: '创建时间', actions: '操作', documentId: '文档 ID', indexedChunks: '已索引分段', content: '内容', document: '文档', stage: '阶段', updatedAt: '更新时间', cancelTask: '取消任务', taskCanceled: '任务已取消', action: '动作', resource: '资源', resourceId: '资源 ID', outcome: '结果', subject: '主体', time: '时间', ready: '已就绪', failedStatus: '失败', processing: '处理中', queued: '排队中', active: '活动', canceled: '已取消' },
  en: { documents: 'Documents', knowledgeBase: 'Knowledge base', chunks: 'Chunks', queryCalls: 'Query calls', completed: 'Completed tasks', inProgress: 'In progress', users: 'Users', failed: 'Failed tasks', workbench: 'Workspace', operations: 'OPERATIONS', overview: 'Live overview of knowledge, retrieval, and indexing.', live: 'Live data', hotQuestions: 'Top / Hot questions', refresh: 'Refresh', noQueries: 'No query records', notifications: 'Notifications', noNotifications: 'No pending notifications', times: 'times', selectFile: 'Choose file', confirmUpload: 'Confirm upload', confirmReplace: 'Confirm replace', reselect: 'Choose another', replaceQueued: 'Document replaced and queued', uploadQueued: 'Document uploaded and queued', chooseFile: 'Choose a file first', replace: 'Replace', rebuild: 'Rebuild', delete: 'Delete', confirmDelete: 'Delete this document?', version: 'Version', status: 'Status', error: 'Error', createdAt: 'Created', actions: 'Actions', documentId: 'Document ID', indexedChunks: 'Indexed chunks', content: 'Content', document: 'Document', stage: 'Stage', updatedAt: 'Updated', cancelTask: 'Cancel task', taskCanceled: 'Task canceled', action: 'Action', resource: 'Resource', resourceId: 'Resource ID', outcome: 'Outcome', subject: 'Subject', time: 'Time', ready: 'Ready', failedStatus: 'Failed', processing: 'Processing', queued: 'Queued', active: 'Active', canceled: 'Canceled' },
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
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [file, setFile] = useState(null);
  const [replaceTarget, setReplaceTarget] = useState(null);
  const [preview, setPreview] = useState(null);
  const load = async () => { setLoading(true); try { setRows((await api('/api/v1/admin/documents')).items); } finally { setLoading(false); } };
  useEffect(() => { load().catch((e) => message.error(e.message)); }, []);
  const selectFile = (next, target = null) => { setFile(next); setReplaceTarget(target); return false; };
  const clearFile = () => { setFile(null); setReplaceTarget(null); };
  const sendFile = async () => {
    if (!file) return message.warning(t.chooseFile);
    const method = replaceTarget ? 'PUT' : 'POST';
    const url = replaceTarget ? `/api/v1/documents/${replaceTarget.id}` : '/api/v1/documents';
    try {
      const body = new FormData(); body.append('file', file);
      await api(url, { method, body });
      message.success(method === 'PUT' ? t.replaceQueued : t.uploadQueued);
      clearFile(); await load();
    } catch (e) { message.error(e.message); }
  };
  const columns = [
    { title: t.document, dataIndex: 'title', ellipsis: true, render: (v, row) => <Button type="link" onClick={async () => setPreview(await api(`/api/v1/documents/${row.id}/content`))}>{v}</Button> },
    { title: t.knowledgeBase, dataIndex: 'knowledge_base', ellipsis: true, render: (v) => v || '—' },
    { title: t.version, dataIndex: 'version', render: (v) => v || 1 },
    { title: t.status, dataIndex: 'status', render: (v) => <StatusTag value={v} t={t} /> },
    { title: t.error, dataIndex: 'error', ellipsis: true, render: (v) => v ? <Typography.Text type="danger">{v}</Typography.Text> : '—' },
    { title: t.createdAt, dataIndex: 'created_at', valueType: 'dateTime' },
    { title: t.actions, render: (_, row) => <Space><Upload showUploadList={false} beforeUpload={(next) => selectFile(next, row)}><Button type="link">{t.replace}</Button></Upload>{row.status === 'failed' && <Button type="link" onClick={async () => { await api(`/api/v1/documents/${row.id}/reindex`, { method: 'POST' }); message.success(t.queued); load(); }}>{t.rebuild}</Button>}<Popconfirm title={t.confirmDelete} onConfirm={async () => { await api(`/api/v1/documents/${row.id}`, { method: 'DELETE' }); message.success(t.delete); load(); }}><Button danger type="link">{t.delete}</Button></Popconfirm></Space> },
  ];
  return <Card title={t.documents} extra={<Upload showUploadList={false} beforeUpload={(next) => selectFile(next)}><Button type="primary">{t.selectFile}</Button></Upload>}>
    {file && <div className="pending-upload"><span>{languageText(t, '已选择：', 'Selected: ')}<b>{file.name}</b>{replaceTarget ? languageText(t, `，将替换「${replaceTarget.title}」`, `, replacing “${replaceTarget.title}”`) : languageText(t, '，将作为新文档上传', ', ready to upload')}</span><Space><Button type="primary" onClick={sendFile}>{replaceTarget ? t.confirmReplace : t.confirmUpload}</Button><Button onClick={clearFile}>{t.reselect}</Button></Space></div>}
    <Table rowKey="id" loading={loading} dataSource={rows} columns={columns} pagination={{ pageSize: 20 }} />
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

export { Dashboard, Documents, Tasks, Logs };
