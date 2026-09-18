const config = require('../../config');
const STORAGE = 'miniapp-pilot-demo-tasks';

Page({
  data: {tasks: [], visibleTasks: [], title: '', filter: 'all', editingId: '', error: ''},
  onLoad() {
    const stored = wx.getStorageSync(STORAGE);
    this.updateTasks(Array.isArray(stored) ? stored : []);
  },
  updateTasks(tasks) {
    wx.setStorageSync(STORAGE, tasks);
    this.setData({tasks});
    this.applyFilter();
  },
  applyFilter() {
    this.setData({visibleTasks: this.data.tasks.filter(task => this.data.filter === 'all'
      || (this.data.filter === 'done' ? task.completed : !task.completed))});
  },
  onTitle(event) { this.setData({title: event.detail.value, error: ''}); },
  save() {
    const title = this.data.title.trim();
    if (!title || title.length > 80) {
      this.setData({error: '请输入 1 至 80 个字符的任务名称'});
      return;
    }
    const tasks = this.data.tasks.map(task => ({...task}));
    if (this.data.editingId) {
      const task = tasks.find(item => item.id === this.data.editingId);
      if (!task) { this.setData({error: '任务已不存在'}); return; }
      task.title = title;
    } else {
      tasks.push({id: `task-${Date.now()}-${tasks.length}`, title, completed: false});
    }
    this.updateTasks(tasks);
    this.setData({title: '', editingId: '', error: ''});
  },
  edit(event) {
    const task = this.data.tasks.find(item => item.id === event.currentTarget.dataset.id);
    if (task) this.setData({title: task.title, editingId: task.id, error: ''});
  },
  toggle(event) {
    this.updateTasks(this.data.tasks.map(task => task.id === event.currentTarget.dataset.id
      ? {...task, completed: !task.completed} : task));
  },
  filter(event) {
    this.setData({filter: event.currentTarget.dataset.filter});
    this.applyFilter();
  },
  reset() {
    this.setData({title: '', editingId: '', filter: 'all', error: ''});
    this.updateTasks([]);
  },
  loadFixtures() {
    if (!config.fixtureBaseUrl) { this.setData({error: '未配置测试数据服务'}); return; }
    wx.request({
      url: config.fixtureBaseUrl + '/tasks', method: 'GET',
      success: response => {
        const tasks = response.data && response.data.tasks;
        if (response.statusCode !== 200 || !Array.isArray(tasks)
          || !tasks.every(task => typeof task.title === 'string' && typeof task.completed === 'boolean')) {
          this.setData({error: '测试数据格式不正确'}); return;
        }
        this.setData({error: '', filter: 'all'});
        this.updateTasks(tasks.map((task, i) => ({...task, id: `fixture-${i}`})));
      },
      fail: () => this.setData({error: '测试数据服务不可用'})
    });
  }
});
