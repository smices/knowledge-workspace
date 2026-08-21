import React, { useEffect, useState } from 'react';
import { ProLayout } from '@ant-design/pro-components';
import { Avatar, Badge, Button, ConfigProvider, Dropdown, Tooltip, theme } from 'antd';
import enUS from 'antd/locale/en_US';
import zhCN from 'antd/locale/zh_CN';
import { BellOutlined, BookOutlined, FileTextOutlined, GlobalOutlined, HistoryOutlined, MoonOutlined, SunOutlined, SyncOutlined, TeamOutlined, ShareAltOutlined, TagsOutlined } from '@ant-design/icons';
import { history, Outlet, useLocation } from '@umijs/max';
import 'antd/dist/reset.css';
import '../styles.css';

const copy = {
  zh: { overview: '总览', documents: '知识库文档', tasks: '任务进度', logs: '审计日志', members: '成员与权限', relations: '关系证据', aliases: '实体称谓', language: 'English', logout: '退出登录', admin: '管理员' },
  en: { overview: 'Overview', documents: 'Documents', tasks: 'Task progress', logs: 'Audit logs', members: 'Members & access', relations: 'Relation evidence', aliases: 'Entity aliases', language: '中文', logout: 'Sign out', admin: 'Admin' },
};
const brand = window.__APP_BRAND__ || { name: 'Knowledge Workspace', mark: 'K', footer: 'Evidence first', primaryColor: '#4f5bd5', logoUrl: '' };

export default function Layout() {
  const { pathname } = useLocation();
  const [dark, setDark] = useState(() => window.localStorage.getItem('sn-admin-theme') === 'dark');
  const [language, setLanguage] = useState(() => window.localStorage.getItem('sn-admin-language') || 'zh');
  const [collapsed, setCollapsed] = useState(false);
  const [notificationCount, setNotificationCount] = useState(0);
  const t = copy[language];
  const toggleLanguage = () => {
    const next = language === 'zh' ? 'en' : 'zh';
    setLanguage(next);
    window.localStorage.setItem('sn-admin-language', next);
    window.dispatchEvent(new CustomEvent('sn-admin-language', { detail: next }));
  };

  useEffect(() => {
    fetch('/api/v1/admin/notifications').then((response) => response.json()).then((data) => setNotificationCount(data.items?.length || 0)).catch(() => {});
  }, []);
  useEffect(() => {
    document.body.classList.toggle('admin-theme-dark', dark);
    return () => document.body.classList.remove('admin-theme-dark');
  }, [dark]);
  useEffect(() => { document.title = `${brand.name} Admin`; }, []);

  const routes = [
    { path: '/', name: t.overview, icon: <BookOutlined /> },
    { path: '/documents', name: t.documents, icon: <FileTextOutlined /> },
    { path: '/tasks', name: t.tasks, icon: <SyncOutlined /> },
    { path: '/members', name: t.members, icon: <TeamOutlined /> },
    { path: '/relations', name: t.relations, icon: <ShareAltOutlined /> },
    { path: '/entity-aliases', name: t.aliases, icon: <TagsOutlined /> },
    { path: '/logs', name: t.logs, icon: <HistoryOutlined /> },
  ];
  const pageName = routes.find((route) => route.path === pathname)?.name || t.overview;
  const userMenu = { items: [{ key: 'logout', label: t.logout }], onClick: ({ key }) => { if (key === 'logout') window.location.assign('/auth/logout'); } };
  const logo = <span className="brand-mark"><span>{brand.mark || 'K'}</span>{brand.logoUrl && <img src={brand.logoUrl} alt="" onError={(event) => { event.currentTarget.hidden = true; }} />}</span>;
  const rightContent = () => <div className="header-actions">
    <Tooltip title={t.language}><Button type="text" icon={<GlobalOutlined />} onClick={toggleLanguage}>{language === 'zh' ? '中' : 'EN'}</Button></Tooltip>
    <Tooltip title={dark ? 'Light theme' : 'Dark theme'}><Button type="text" icon={dark ? <SunOutlined /> : <MoonOutlined />} onClick={() => { const next = !dark; setDark(next); window.localStorage.setItem('sn-admin-theme', next ? 'dark' : 'light'); }} /></Tooltip>
    <Badge count={notificationCount} size="small"><Button type="text" icon={<BellOutlined />} aria-label="Notifications" /></Badge>
    <Dropdown menu={userMenu}><button className="user-button"><Avatar size="small">A</Avatar><span>{t.admin}</span></button></Dropdown>
  </div>;

  return <ConfigProvider locale={language === 'zh' ? zhCN : enUS} theme={{ algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm, token: { colorPrimary: brand.primaryColor || '#4f5bd5', borderRadius: 10 } }}>
    <div className={`admin-app${dark ? ' theme-dark' : ''}${collapsed ? ' sider-collapsed' : ''}`} style={{ '--brand-primary': brand.primaryColor || '#4f5bd5' }}>
      <div className="app-header"><div className="app-header-title">{brand.name}</div>{rightContent()}</div>
      <ProLayout
        title={brand.name}
        logo={logo}
        layout="side"
        collapsed={collapsed}
        onCollapse={setCollapsed}
        navTheme={dark ? 'realDark' : 'light'}
        headerTheme="light"
        fixedHeader
        fixSiderbar
        location={{ pathname }}
        route={{ routes }}
        rightContentRender={false}
        menuItemRender={(item, dom) => <a href={item.path} onClick={(event) => { event.preventDefault(); history.push(item.path); }}>{dom}</a>}
        headerRender={(_, defaultDom) => <div className="pro-header">{defaultDom}</div>}
        footerRender={() => <div className="admin-footer">{brand.name}{brand.footer ? ` · ${brand.footer}` : ''}</div>}
      >
        <div className="page-content"><div className="breadcrumb">{brand.name} <span>/</span> {pageName}</div><Outlet /></div>
      </ProLayout>
    </div>
  </ConfigProvider>;
}
