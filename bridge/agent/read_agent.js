// 欢太健康桥 · Frida 只读代理
// 注入运行中的欢太健康进程，经其自身已加载的 SQLCipher 打开主库 database.db，
// 只执行只读 SELECT。注册依赖用 App 自己的 DelegateInit（幂等），不修改源数据。
import Java from 'frida-java-bridge';

let roomDb = null;

function ensureDb() {
  if (roomDb !== null) return roomDb;
  const Clazz = Java.use('java.lang.Class');
  const ctx = Java.use('android.app.ActivityThread').currentApplication().getApplicationContext();
  const BD = Java.use('com.heytap.databaseengineservice.depend.BusinessDelegate');
  const loader = BD.class.getClassLoader();
  // 运行 App 自己的依赖注册（幂等：重复调用只是重新登记委托实例）
  Clazz.forName('com.health.database.depend.DelegateInit', true, loader);
  Java.use('com.health.database.depend.DelegateInit').$new().call();
  const inst = Java.use('com.heytap.databaseengineservice.db.AppDatabase').getInstance(ctx);
  roomDb = Java.cast(inst, Java.use('androidx.room.RoomDatabase'));
  return roomDb;
}

rpc.exports = {
  // 预热：注册依赖并打开主库。返回数据库路径。
  ensure() {
    return new Promise((resolve, reject) => Java.perform(() => {
      try {
        const db = ensureDb();
        const c = db.query.overload('java.lang.String', '[Ljava.lang.Object;')
          .call(db, 'SELECT 1', null);
        try { c.moveToNext(); } finally { c.close(); }
        resolve({ ready: true });
      } catch (e) { reject(new Error(String(e) + '\n' + (e.stack || ''))); }
    }));
  },

  // 只读查询，返回 {columns, rows}。rows 为数组的数组，按列顺序。
  select(sql, maxRows) {
    return new Promise((resolve, reject) => Java.perform(() => {
      try {
        const db = ensureDb();
        const c = db.query.overload('java.lang.String', '[Ljava.lang.Object;').call(db, sql, null);
        const nc = c.getColumnCount();
        const cols = []; for (let k = 0; k < nc; k++) cols.push(String(c.getColumnName(k)));
        const rows = []; let i = 0;
        try {
          while (c.moveToNext() && i < maxRows) {
            const r = [];
            for (let k = 0; k < nc; k++) {
              const t = c.getType(k);
              if (t === 0) r.push(null);
              else if (t === 1) r.push(c.getLong(k));
              else if (t === 2) r.push(c.getDouble(k));
              else if (t === 4) r.push('<blob:' + c.getBlob(k).length + '>');
              else r.push(String(c.getString(k)));
            }
            rows.push(r); i++;
          }
        } finally { c.close(); }
        resolve({ columns: cols, rows: rows });
      } catch (e) { reject(new Error(String(e) + '\n' + (e.stack || ''))); }
    }));
  },

  // 列出主库全部表名。
  tables() {
    return new Promise((resolve, reject) => Java.perform(() => {
      try {
        const db = ensureDb();
        const c = db.query.overload('java.lang.String', '[Ljava.lang.Object;')
          .call(db, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name", null);
        const out = [];
        try { while (c.moveToNext()) out.push(String(c.getString(0))); } finally { c.close(); }
        resolve(out);
      } catch (e) { reject(new Error(String(e) + '\n' + (e.stack || ''))); }
    }));
  }
};
