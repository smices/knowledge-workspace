import { defineConfig } from '@umijs/max';

export default defineConfig({
  title: 'Knowledge Workspace',
  base: '/admin/',
  publicPath: '/admin/',
  hash: true,
  headScripts: [{ src: '/brand.js' }],
  antd: {},
  routes: [
    { path: '/', component: '@/pages/index' },
    { path: '/documents', component: '@/pages/documents' },
    { path: '/tasks', component: '@/pages/tasks' },
    { path: '/members', component: '@/pages/members' },
    { path: '/relations', component: '@/pages/relations' },
    { path: '/entity-aliases', component: '@/pages/entity-aliases' },
    { path: '/logs', component: '@/pages/logs' },
    { path: '*', redirect: '/' },
  ],
});
