/* 页面访问口令：轻量门禁，防止陌生人随手打开页面查看内容
 * 注意：这是前端口令，只能挡住"随手打开"的人，懂技术的人查看源码仍可绕过；
 *       如需真正的访问控制，请改用 Cloudflare Pages + 邮箱白名单（详见 README.md）
 */
(function () {
  var PWD = '12345678';        // ← 访问口令，改成你自己的（改完需重新提交推送）
  var FLAG = 'kl8_gate_v1';    // 本机记住"已通过"的标记名


  var passed = false;
  // 本机调试（本地服务或直接打开文件）不拦，避免自己每次都要输口令
  if (/^(localhost|127\.0\.0\.1)$/.test(location.hostname) || location.protocol === 'file:') return;

  try { passed = localStorage.getItem(FLAG) === '1'; } catch (e) { /* 隐私模式下会失败，按未通过处理 */ }
  if (passed) return;          // 已通过过：直接显示页面

  // 未通过：先把整页藏起来，避免内容一闪而过
  document.documentElement.style.visibility = 'hidden';

  document.addEventListener('DOMContentLoaded', function () {
    var box = document.createElement('div');
    box.id = 'gateBox';
    box.style.cssText = [
      'position:fixed', 'inset:0', 'z-index:99999',
      'background:radial-gradient(800px 420px at 50% 20%,rgba(154,106,26,.08),transparent 60%),#efeae2',
      'display:flex', 'align-items:center', 'justify-content:center', 'padding:24px'
    ].join(';');
    box.innerHTML =
      '<div style="width:100%;max-width:360px;text-align:center;font-family:\'Microsoft YaHei\',sans-serif;color:#3a352e;' +
        'background:#faf7f1;border-radius:16px;padding:26px 22px;box-shadow:0 8px 24px rgba(90,76,58,.14)">' +
        '<div style="font-size:20px;font-weight:800;color:#8a5a12;margin-bottom:6px">快乐8 预测台</div>' +
        '<div style="font-size:12px;color:#7d766a;margin-bottom:18px">请输入访问口令</div>' +
        '<input id="gatePwd" type="password" autocomplete="off" ' +
          'style="width:100%;box-sizing:border-box;padding:11px 13px;border-radius:8px;border:1px solid rgba(58,53,46,.16);' +
          'background:#fffdf8;color:#3a352e;font-size:14px;outline:none">' +
        '<button id="gateGo" style="margin-top:12px;width:100%;padding:11px;border:none;border-radius:8px;cursor:pointer;' +
          'background:linear-gradient(135deg,#a97a2a,#6d5a9c);color:#fff;font-size:14px;font-weight:700">进入</button>' +
        '<div id="gateMsg" style="margin-top:10px;font-size:12px;color:#b04a3a;min-height:16px"></div>' +
      '</div>';
    document.body.appendChild(box);

    var input = document.getElementById('gatePwd');
    var msg = document.getElementById('gateMsg');

    // 校验口令：正确则记住并移除遮罩，错误则提示
    function submit() {
      if (input.value.trim() === PWD) {
        try { localStorage.setItem(FLAG, '1'); } catch (e) {}
        box.remove();
        document.documentElement.style.visibility = 'visible';
      } else {
        msg.textContent = '口令不正确，请重试';
        input.value = '';
      }
    }
    document.getElementById('gateGo').addEventListener('click', submit);
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') submit(); });

    // 遮罩层就绪后再显示页面容器，保证口令框可见
    document.documentElement.style.visibility = 'visible';
    input.focus();
  });
})();
