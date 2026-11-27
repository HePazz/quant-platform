/* 量化平台前端：只读仓库里的数据文件；改自选股、手动触发才调 GitHub API。
 * 令牌（PAT）只存在本浏览器的 localStorage 里，除了发给 api.github.com
 * 不会去任何别处；页面本身不存、不发任何密钥。
 */
(function () {
  'use strict';

  var API = 'https://api.github.com';
  var LS_PAT = 'qp_pat';
  var LS_OWNER = 'qp_owner';
  var LS_REPO = 'qp_repo';

  /* 策略英文 key → 中文名（取自上游 AlphaSift strategies/README.md 的官方名称；
   * 新策略不在表里时回退显示原 key）。 */
  var STRATEGY_NAMES = {
    balanced_alpha: '均衡多因子',
    blue_chip_income: '蓝筹收益质量',
    capital_heat: '资金热度',
    dual_low: '双低选股',
    low_volatility_quality: '低波质量',
    momentum_quality: '趋势质量',
    oversold_reversal: '超跌反转',
    quality_value: '稳健价值',
    shrink_pullback: '缩量回踩',
    volume_breakout: '放量突破',
  };

  var state = {
    owner: localStorage.getItem(LS_OWNER) || 'HePazz',
    repo: localStorage.getItem(LS_REPO) || 'quant-platform',
    pat: localStorage.getItem(LS_PAT) || '',
    data: null,
    watchlist: [],
  };

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function showError(msg) {
    var b = $('error-banner');
    b.textContent = msg;
    b.hidden = !msg;
  }
  function setStatus(text) { $('data-status').textContent = text; }

  /* ---------------- 数据加载与渲染 ---------------- */

  function loadJson(url) {
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error('读数据失败：' + r.status + ' ' + url);
      return r.json();
    });
  }

  function loadLatest() {
    return loadJson('data/latest.json');
  }

  function render(data) {
    state.data = data;
    state.watchlist = (data.watchlist || []).slice();
    $('sample-banner').hidden = !data.sample;
    var gen = data.generated_at ? new Date(data.generated_at) : null;
    setStatus(
      (data.trade_date ? data.trade_date + ' ' : '') +
      (gen ? '更新于 ' + gen.toLocaleString('zh-CN', { hour12: false }) : '暂无数据')
    );
    renderPicks(data.picks);
    renderReports(data.reports, data.trade_date);
    renderTrack(data.track_record);
    renderMarket(data.market);
    renderPosition(data.position, data.risk);
    renderBacktest(data.backtest, data.factors);
    renderWatchlist();
  }

  function renderPicks(picks) {
    $('picks-date').textContent = picks && picks.trade_date ? '· ' + picks.trade_date : '';
    var brief = $('ai-brief');
    if (picks && picks.ai_brief) {
      brief.textContent = picks.ai_brief;
      brief.hidden = false;
    } else {
      brief.hidden = true;
    }
    var box = $('picks-list');
    var strategies = (picks && picks.strategies) || [];
    if (!strategies.length) {
      box.innerHTML = '<p class="muted">暂无选股数据（等今天 18:30 的选股任务跑完）。</p>';
    } else {
      box.innerHTML = strategies.map(function (s) {
        var rows = (s.picks || []).map(function (p) {
          return '<tr><td>' + esc(p.rank) + '</td><td><b>' + esc(p.code) + '</b><br>' +
            esc(p.name) + '</td><td>' + esc(p.final_score) + '</td><td>' + esc(p.industry || '') +
            '</td><td>' + esc(p.risk_level || '') + '</td><td>' + esc(p.ranking_reason || '') + '</td></tr>';
        }).join('');
        return '<h3>' + esc(STRATEGY_NAMES[s.strategy] || s.strategy) + '（' + (s.picks || []).length + ' 只）</h3>' +
          '<table><thead><tr><th>#</th><th>代码 / 名称</th><th>得分</th><th>行业</th><th>风险</th><th>入选理由</th></tr></thead><tbody>' +
          rows + '</tbody></table>';
      }).join('');
    }
    var risks = (picks && picks.announcement_risks) || [];
    $('announcement-risks').hidden = !risks.length;
    $('risks-list').innerHTML = risks.map(function (r) {
      return '<li>' + esc(typeof r === 'string' ? r : (r.text || r.title || JSON.stringify(r))) + '</li>';
    }).join('');
    if (picks && picks.stale) {
      $('picks-note').textContent = '注意：数据包是旧的（' + esc(picks.stale_reason || 'stale') + '），今天的结果可能不全。';
    }
  }

  function renderReports(reports, tradeDate) {
    $('reports-date').textContent = tradeDate ? '· ' + tradeDate : '';
    var box = $('reports-list');
    var codes = Object.keys(reports || {});
    if (!codes.length) {
      box.innerHTML = '<p class="muted">暂无研报（等今天 18:00 的分析任务跑完）。</p>';
      return;
    }
    box.innerHTML = codes.map(function (code) {
      var r = reports[code] || {};
      var link = r.html ? ' <a href="' + esc(r.html) + '" target="_blank" rel="noopener">看完整报告</a>' : '';
      return '<div class="report"><h3>' + esc(code) + ' ' + esc(r.name || '') +
        (r.score != null ? ' <span class="score">评分 ' + esc(r.score) + '</span>' : '') + '</h3>' +
        (r.advice ? '<p><b>建议：</b>' + esc(r.advice) + '</p>' : '') +
        (r.summary ? '<p>' + esc(r.summary) + '</p>' : '') + link + '</div>';
    }).join('');
  }

  function renderTrack(track) {
    var box = $('track-list');
    var stats = $('strategy-stats');
    if (!track || !track.history || !track.history.length) {
      box.innerHTML = '<p class="muted">还没有验收数据：选股跑过、且到 T+5 后才有数字。</p>';
      stats.innerHTML = '';
      return;
    }
    box.innerHTML = track.history.slice(-10).reverse().map(function (day) {
      var comps = (day.track_comparison || []).map(function (c) {
        return esc(typeof c === 'string' ? c : JSON.stringify(c));
      }).join('；');
      return '<p><b>' + esc(day.date) + '</b> ' + (comps || '待验收') + '</p>';
    }).join('');
    var ss = track.strategy_stats || {};
    var names = Object.keys(ss);
    stats.innerHTML = names.length
      ? '<h3>各策略胜率 / 盈亏比（有已实现收益的才算）</h3>' + names.map(function (n) {
          var s = ss[n];
          return '<p>' + esc(n) + '：胜率 ' + esc(s.win_rate) + '，盈亏比 ' +
            esc(s.payoff_ratio == null ? '—' : s.payoff_ratio) + '（' + esc(s.observations) + ' 只）</p>';
        }).join('')
      : '';
  }

  function renderMarket(market) {
    $('market-review').textContent = (market && market.review) || '暂无';
  }

  function renderPosition(position, risk) {
    var box = $('position-list');
    var positions = (position && position.positions) || {};
    var codes = Object.keys(positions);
    box.innerHTML = codes.length
      ? '<table><thead><tr><th>代码</th><th>建议仓位</th><th>止损</th><th>跟随止盈</th><th>依据</th></tr></thead><tbody>' +
        codes.map(function (code) {
          var p = positions[code];
          return '<tr><td><b>' + esc(code) + '</b></td><td>' + esc(p.suggested_pct) + '%</td><td>-' +
            esc(p.stop_loss_pct) + '%</td><td>+' + esc(p.trailing_take_profit_pct) + '%</td><td>' +
            esc(p.note || p.source || '') + '</td></tr>';
        }).join('') + '</tbody></table>'
      : '<p class="muted">暂无仓位建议（等跟踪数据积累）。</p>';
    var checks = (risk && risk.account_checks) || [];
    $('risk-checks').innerHTML = checks.length
      ? '<h3>账户级风控线（自己对照券商 App）</h3><ul>' +
        checks.map(function (c) {
          return '<li>' + esc(c.line) + ' ' + esc(c.limit_pct) + '%：' + esc(c.action) + '</li>';
        }).join('') + '</ul>'
      : '';
  }

  function renderBacktest(backtest, factors) {
    $('backtest-box').innerHTML = backtest
      ? '<p><b>回测：</b>' + (backtest.cumulative_net_pct == null
          ? '暂无结果。' + esc(backtest.note || '')
          : '累计净收益 ' + esc(backtest.cumulative_net_pct) + '%，最大回撤 ' +
            esc(backtest.max_drawdown_pct) + '%，夏普 ' + esc(backtest.sharpe) + '。' +
            esc(backtest.note || '')) + '</p>'
      : '<p class="muted">暂无回测。</p>';
    $('factors-box').innerHTML = factors
      ? '<p><b>因子检验：</b>' + (factors.ic == null
          ? '样本不足（' + esc(factors.observations) + ' 条）。' + esc(factors.note || '')
          : 'IC ' + esc(factors.ic) + '，ICIR ' + esc(factors.icir) + '，结论：' +
            esc(factors.verdict) + '。' + esc(factors.note || '')) + '</p>'
      : '<p class="muted">暂无因子检验。</p>';
  }

  /* ---------------- 自选股管理 ---------------- */

  function renderWatchlist() {
    var box = $('watchlist-list');
    box.innerHTML = state.watchlist.length
      ? state.watchlist.map(function (code, i) {
          return '<span class="chip">' + esc(code) +
            ' <button type="button" data-i="' + i + '" class="chip-x" title="移除">×</button></span>';
        }).join('')
      : '<p class="muted">自选股为空。</p>';
    box.querySelectorAll('.chip-x').forEach(function (btn) {
      btn.addEventListener('click', function () {
        state.watchlist.splice(Number(btn.getAttribute('data-i')), 1);
        renderWatchlist();
      });
    });
  }

  function gh(path, options) {
    options = options || {};
    options.headers = Object.assign({
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    }, options.headers || {});
    if (state.pat) options.headers['Authorization'] = 'Bearer ' + state.pat;
    return fetch(API + path, options).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          throw new Error('GitHub API ' + r.status + '：' + (body.message || '请求失败'));
        });
      }
      if (r.status === 204) return null;
      return r.json();
    });
  }

  function needPat() {
    if (!state.pat) {
      $('watchlist-msg').textContent = '先在设置里贴 GitHub 令牌，才能改自选股。';
      openSettings();
      return true;
    }
    return false;
  }

  function saveWatchlist() {
    if (needPat()) return;
    var msg = $('watchlist-msg');
    var value = state.watchlist.join(',');
    msg.textContent = '正在加密并写回仓库…';
    if (typeof window.sodium === 'undefined') {
      msg.textContent = '加密库（libsodium）没加载出来，检查网络后刷新再试。';
      return;
    }
    window.sodium.ready.then(function () {
      var sodium = window.sodium;
      return gh('/repos/' + state.owner + '/' + state.repo + '/actions/secrets/public-key')
        .then(function (pub) {
          var key = sodium.from_base64(pub.key, sodium.base64_variants.ORIGINAL);
          var sealed = sodium.crypto_box_seal(
            sodium.from_string(value), key);
          var encrypted = sodium.to_base64(sealed, sodium.base64_variants.ORIGINAL);
          return gh('/repos/' + state.owner + '/' + state.repo + '/actions/secrets/STOCK_LIST', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ encrypted_value: encrypted, key_id: pub.key_id }),
          });
        });
    }).then(function () {
      msg.textContent = '已保存到仓库 STOCK_LIST，下次定时运行生效。';
    }).catch(function (e) {
      msg.textContent = '保存失败：' + e.message + '（令牌需要 repo 权限；令牌本身没有被发到别处）';
    });
  }

  function triggerWorkflow(file, inputs, label) {
    if (needPat()) return;
    var msg = $('watchlist-msg');
    msg.textContent = '正在触发' + label + '…';
    gh('/repos/' + state.owner + '/' + state.repo + '/actions/workflows/' + file + '/dispatches', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ref: 'main', inputs: inputs || {} }),
    }).then(function () {
      msg.textContent = label + '已触发，去仓库 Actions 页看进度（一般要几分钟到十几分钟）。';
    }).catch(function (e) {
      msg.textContent = '触发失败：' + e.message;
    });
  }

  /* ---------------- 历史切换 ---------------- */

  function loadHistory() {
    loadJson('data/history.json').then(function (h) {
      var sel = $('history-select');
      var dates = (h.dates || []).slice().reverse();
      sel.innerHTML = dates.map(function (d) {
        return '<option value="' + esc(d) + '"' + (d === (state.data && state.data.trade_date) ? ' selected' : '') + '>' + esc(d) + '</option>';
      }).join('');
      if (!dates.length) sel.innerHTML = '<option>暂无历史</option>';
    }).catch(function () {
      $('history-select').innerHTML = '<option>暂无历史</option>';
    });
  }

  /* ---------------- 设置 ---------------- */

  var dlg = $('settings-dialog');
  function openSettings() {
    $('set-owner').value = state.owner;
    $('set-repo').value = state.repo;
    $('set-pat').value = state.pat;
    $('settings-msg').textContent = '';
    if (typeof dlg.showModal === 'function') dlg.showModal();
  }

  /* ---------------- 启动 ---------------- */

  $('btn-refresh').addEventListener('click', function () {
    setStatus('刷新中…');
    loadLatest().then(render).catch(function (e) { showError(e.message); });
  });
  $('btn-settings').addEventListener('click', openSettings);
  $('btn-close-settings').addEventListener('click', function () { dlg.close(); });
  $('btn-save-settings').addEventListener('click', function () {
    state.owner = $('set-owner').value.trim() || 'HePazz';
    state.repo = $('set-repo').value.trim() || 'quant-platform';
    state.pat = $('set-pat').value.trim();
    localStorage.setItem(LS_OWNER, state.owner);
    localStorage.setItem(LS_REPO, state.repo);
    if (state.pat) localStorage.setItem(LS_PAT, state.pat);
    else localStorage.removeItem(LS_PAT);
    $('settings-msg').textContent = '已保存（令牌只存在这个浏览器里）。';
  });
  $('btn-clear-pat').addEventListener('click', function () {
    state.pat = '';
    localStorage.removeItem(LS_PAT);
    $('set-pat').value = '';
    $('settings-msg').textContent = '令牌已清除。';
  });
  $('btn-test-pat').addEventListener('click', function () {
    state.pat = $('set-pat').value.trim();
    if (!state.pat) { $('settings-msg').textContent = '先贴令牌。'; return; }
    $('settings-msg').textContent = '正在测试…';
    gh('/user').then(function (u) {
      $('settings-msg').textContent = '令牌有效，登录账号是 ' + (u.login || '未知') + '。';
    }).catch(function (e) {
      $('settings-msg').textContent = '测试失败：' + e.message;
    });
  });
  $('btn-add-code').addEventListener('click', function () {
    var input = $('new-code');
    var code = input.value.trim();
    if (!/^\d{6}$/.test(code)) {
      $('watchlist-msg').textContent = '代码要 6 位数字，如 000001。';
      return;
    }
    if (state.watchlist.indexOf(code) === -1) state.watchlist.push(code);
    input.value = '';
    renderWatchlist();
  });
  $('btn-save-watchlist').addEventListener('click', saveWatchlist);
  $('btn-run-analysis').addEventListener('click', function () {
    triggerWorkflow('daily-analysis.yml', { mode: 'full' }, '每日分析');
  });
  $('btn-run-picks').addEventListener('click', function () {
    triggerWorkflow('daily-picks.yml', { markets: 'cn' }, '每日选股');
  });
  $('history-select').addEventListener('change', function (e) {
    var date = e.target.value;
    if (!date || date === '暂无历史') return;
    loadJson('data/days/' + date + '.json').then(render).catch(function (err) {
      showError(err.message);
    });
  });

  loadLatest().then(render).catch(function (e) {
    showError('数据还没生成：' + e.message + '（等第一次定时任务跑完，或点设置检查仓库名。）');
    renderWatchlist();
  });
  loadHistory();
})();
