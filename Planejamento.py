import os
import sqlite3
from datetime import date, datetime
from calendar import monthrange
from pathlib import Path
from flask import Flask, g, render_template, request, redirect, url_for, jsonify, flash, abort

# --- Paths e Flask ---
APP_DIR = Path(__file__).parent.resolve()
DB_PATH = APP_DIR / 'planner.db'
TEMPLATES_DIR = APP_DIR / 'templates'
STATIC_DIR = APP_DIR / 'static'

for p in [TEMPLATES_DIR, STATIC_DIR / 'js']:
    p.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, template_folder=str(TEMPLATES_DIR), static_folder=str(STATIC_DIR))
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret')

# Expor date/datetime para Jinja
app.jinja_env.globals.update(date=date, datetime=datetime)

# --- DB helpers ---
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def _ensure_schema_and_profile():
    db = get_db()
    db.executescript(
        """
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS calendar (
            cdate TEXT PRIMARY KEY,
            note  TEXT,
            status TEXT CHECK(status IN ('none','ok','miss')) DEFAULT 'none'
        );

        CREATE TABLE IF NOT EXISTS simulados (
            id INTEGER PRIMARY KEY,
            sdate TEXT NOT NULL,
            q INTEGER NOT NULL,
            a INTEGER NOT NULL,
            disc TEXT
        );

        CREATE TABLE IF NOT EXISTS taf_summary (
            adate TEXT PRIMARY KEY,
            running_km REAL,
            running_minutes INTEGER,
            pushups INTEGER,
            situps INTEGER,
            pullups INTEGER,
            weight REAL,
            bmi REAL
        );

        CREATE TABLE IF NOT EXISTS user_profile (
            id INTEGER PRIMARY KEY CHECK (id=1),
            height_m REAL,
            birthdate TEXT
        );
        """
    )

    # Migrações brandas de colunas (se faltar, adiciona)
    cols_taf = {r['name'] for r in db.execute("PRAGMA table_info('taf_summary')").fetchall()}
    if 'bmi' not in cols_taf:
        db.execute("ALTER TABLE taf_summary ADD COLUMN bmi REAL")

    cols_prof = {r['name'] for r in db.execute("PRAGMA table_info('user_profile')").fetchall()}
    if 'height_m' not in cols_prof:
        db.execute("ALTER TABLE user_profile ADD COLUMN height_m REAL")
    if 'birthdate' not in cols_prof:
        db.execute("ALTER TABLE user_profile ADD COLUMN birthdate TEXT")

    # Seed perfil com sua altura e data de nascimento (19/06/1999)
    row = db.execute("SELECT id FROM user_profile WHERE id=1").fetchone()
    if not row:
        db.execute(
            "INSERT INTO user_profile (id, height_m, birthdate) VALUES (1, ?, ?)",
            (1.71, '1999-06-19')
        )
    else:
        prof = db.execute("SELECT height_m, birthdate FROM user_profile WHERE id=1").fetchone()
        if prof['height_m'] is None:
            db.execute("UPDATE user_profile SET height_m=? WHERE id=1", (1.71,))
        if prof['birthdate'] is None:
            db.execute("UPDATE user_profile SET birthdate=? WHERE id=1", ('1999-06-19',))
    db.commit()

@app.before_request
def init_db():
    _ensure_schema_and_profile()

# --- Util ---
MONTHS_PT = [
    'January','February','March','April','May','June',
    'July','August','September','October','November','December'
]

def _isodate_ok(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except Exception:
        return False

def _age_from_dob(birthdate_iso: str) -> int:
    dob = datetime.strptime(birthdate_iso, "%Y-%m-%d").date()
    today = date.today()
    years = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    return years

# --- Rotas principais ---
@app.route('/')
def home():
    return redirect(url_for('calendar_view'))

# -------------------- CALENDÁRIO --------------------
@app.get('/calendar')
def calendar_view():
    today = date.today()
    month = int(request.args.get('month', today.month - 1))  # 0-based
    year = int(request.args.get('year', today.year))

    first_weekday, days_in_month = monthrange(year, month + 1)  # monthrange usa 1-12
    start_date = date(year, month + 1, 1)

    db = get_db()
    entries = {
        r['cdate']: r
        for r in db.execute(
            'SELECT * FROM calendar WHERE cdate BETWEEN ? AND ?',
            (start_date.isoformat(), date(year, month + 1, days_in_month).isoformat())
        ).fetchall()
    }

    # Monta grid (domingo primeiro)
    grid = []
    pad_start = (first_weekday - 6) % 7
    for _ in range(pad_start):
        grid.append({'in_month': False})
    for d in range(1, days_in_month + 1):
        cdate = date(year, month + 1, d).isoformat()
        row = entries.get(cdate)
        grid.append({
            'in_month': True,
            'day': d,
            'cdate': cdate,
            'note': (row['note'] if row else ''),
            'status': (row['status'] if row else 'none')
        })
    while len(grid) % 7 != 0:
        grid.append({'in_month': False})

    stats = {
        'ok': sum(1 for c in grid if c.get('status') == 'ok'),
        'miss': sum(1 for c in grid if c.get('status') == 'miss'),
        'planned': sum(1 for c in grid if (c.get('note') or '').strip() and c.get('status') == 'none')
    }

    prev_m = (month - 1) % 12
    prev_y = year - 1 if month == 0 else year
    next_m = (month + 1) % 12
    next_y = year + 1 if month == 11 else year

    return render_template(
        'calendar/design_a.html',
        month=month,
        year=year,
        month_name=MONTHS_PT[month],
        months=list(enumerate(MONTHS_PT)),
        years=[today.year - 1, today.year, today.year + 1, today.year + 2],
        grid=grid,
        stats=stats,
        prev_m=prev_m, prev_y=prev_y,
        next_m=next_m, next_y=next_y
    )

@app.post('/calendar/save')
def calendar_save():
    month = int(request.args['month'])
    year = int(request.args['year'])
    _, days_in_month = monthrange(year, month + 1)
    db = get_db()

    for d in range(1, days_in_month + 1):
        cdate = date(year, month + 1, d).isoformat()
        note = (request.form.get(f'note_{cdate}') or '').strip()
        prev = db.execute('SELECT status FROM calendar WHERE cdate=?', (cdate,)).fetchone()
        status = request.form.get(f'status_{cdate}') or (prev['status'] if prev else 'none')

        row = db.execute('SELECT cdate FROM calendar WHERE cdate=?', (cdate,)).fetchone()
        if row:
            db.execute('UPDATE calendar SET note=?, status=? WHERE cdate=?', (note, status, cdate))
        else:
            if note or status != 'none':
                db.execute('INSERT INTO calendar (cdate, note, status) VALUES (?,?,?)', (cdate, note, status))
    db.commit()
    flash('Calendar saved.')
    return redirect(url_for('calendar_view', month=month, year=year))

# Limpar um dia do calendário (nota e status)
@app.post('/calendar/clear/<cdate>')
def calendar_clear_day(cdate):
    if not _isodate_ok(cdate):
        abort(400, 'invalid date')
    db = get_db()
    db.execute('DELETE FROM calendar WHERE cdate=?', (cdate,))
    db.commit()
    flash(f'Dia do calendário limpo: {cdate}')
    try:
        d = datetime.strptime(cdate, "%Y-%m-%d").date()
        return redirect(url_for('calendar_view', month=d.month - 1, year=d.year))
    except Exception:
        return redirect(url_for('calendar_view'))

# -------------------- SIMULADOS --------------------
@app.get('/simulados')
def simulados_view():
    db = get_db()
    rows = db.execute('SELECT * FROM simulados ORDER BY sdate DESC, id DESC').fetchall()
    count = len(rows)
    if count:
        percents = [100.0 * r['a'] / r['q'] for r in rows if r['q']]
        avg = sum(percents) / len(percents) if percents else 0.0
        best = max(percents) if percents else 0.0
        worst = min(percents) if percents else 0.0
    else:
        avg = best = worst = 0.0
    return render_template('simulados/design_b.html', rows=rows, stats={'count': count, 'avg': avg, 'best': best, 'worst': worst})

@app.post('/simulados')
def simulados_add():
    db = get_db()
    sdate = request.form['sdate']
    q = int(request.form['q'])
    a = int(request.form['a'])
    disc = (request.form.get('disc') or '').strip()
    if q <= 0 or a < 0 or a > q:
        flash('Invalid values.')
        return redirect(url_for('simulados_view'))
    db.execute('INSERT INTO simulados (sdate,q,a,disc) VALUES (?,?,?,?)', (sdate, q, a, disc))
    db.commit()
    flash('Test added.')
    return redirect(url_for('simulados_view'))

@app.get('/simulados/del/<int:rid>')
def simulados_del(rid: int):
    db = get_db()
    db.execute('DELETE FROM simulados WHERE id=?', (rid,))
    db.commit()
    flash('Deleted.')
    return redirect(url_for('simulados_view'))

# Apagar todos os simulados de uma data específica
@app.post('/simulados/del-date/<sdate>')
def simulados_delete_by_date(sdate):
    if not _isodate_ok(sdate):
        abort(400, 'invalid date')
    db = get_db()
    db.execute('DELETE FROM simulados WHERE sdate=?', (sdate,))
    db.commit()
    flash(f'Simulados removidos na data: {sdate}')
    return redirect(url_for('simulados_view'))

# -------------------- TAF --------------------
@app.get('/taf')
def taf_view():
    db = get_db()
    rows = db.execute('SELECT * FROM taf_summary ORDER BY adate DESC').fetchall()
    profile = db.execute('SELECT * FROM user_profile WHERE id=1').fetchone()
    height_m = profile['height_m']
    birthdate = profile['birthdate']
    age_now = _age_from_dob(birthdate)

    return render_template('taf/design_c.html', rows=rows, stats={}, profile={'height_m': height_m, 'birthdate': birthdate, 'age': age_now})

@app.post('/taf')
def taf_add():
    db = get_db()
    adate = request.form.get('date') or date.today().isoformat()
    running_km = request.form.get('running_km')
    pushups = request.form.get('pushups')
    situps = request.form.get('situps')
    pullups = request.form.get('pullups')
    weight = request.form.get('weight')

    running_km = float(running_km) if running_km else None
    pushups = int(pushups) if pushups else None
    situps = int(situps) if situps else None
    pullups = int(pullups) if pullups else None
    weight_val = float(weight) if weight else None

    db.execute(
        """
        INSERT INTO taf_summary (adate,running_km,running_minutes,pushups,situps,pullups,weight)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(adate) DO UPDATE SET
          running_km=COALESCE(excluded.running_km, taf_summary.running_km),
          running_minutes=COALESCE(excluded.running_minutes, taf_summary.running_minutes),
          pushups=COALESCE(excluded.pushups, taf_summary.pushups),
          situps=COALESCE(excluded.situps, taf_summary.situps),
          pullups=COALESCE(excluded.pullups, taf_summary.pullups),
          weight=COALESCE(excluded.weight, taf_summary.weight)
        """,
        (adate, running_km, None, pushups, situps, pullups, weight_val)
    )

    # calcular e salvar BMI se houver peso
    if weight_val is not None:
        profile = db.execute('SELECT * FROM user_profile WHERE id=1').fetchone()
        height_m = profile['height_m']
        bmi = float(weight_val) / (height_m ** 2)
        db.execute('UPDATE taf_summary SET bmi=? WHERE adate=?', (bmi, adate))

    db.commit()
    flash('Day saved.')
    return redirect(url_for('taf_view'))

# API para gráficos (BMI por padrão)
@app.get('/taf/data')
def taf_data():
    metric = request.args.get('metric', 'BMI')
    db = get_db()
    rows = db.execute(
        'SELECT adate, running_km, pushups, situps, pullups, weight, bmi FROM taf_summary ORDER BY adate'
    ).fetchall()
    labels = [r['adate'] for r in rows]
    if metric == 'Push-ups':
        values = [r['pushups'] or 0 for r in rows]
    elif metric == 'Sit-ups':
        values = [r['situps'] or 0 for r in rows]
    elif metric == 'Pull-ups':
        values = [r['pullups'] or 0 for r in rows]
    elif metric == 'Running':
        values = [float(r['running_km'] or 0) for r in rows]
    else:  # BMI
        values = [float(r['bmi'] or 0) for r in rows]
    return jsonify({'labels': labels, 'values': values})

# --- Deleções backend para TAF ---
# Apagar um dia específico do TAF
@app.post('/taf/del/<adate>')
def taf_delete_day(adate):
    if not _isodate_ok(adate):
        abort(400, 'invalid date')
    db = get_db()
    db.execute('DELETE FROM taf_summary WHERE adate=?', (adate,))
    db.commit()
    flash(f'TAF removido: {adate}')
    return redirect(url_for('taf_view'))

# Apagar intervalo de dias do TAF (inclusive)
@app.post('/taf/del-range')
def taf_delete_range():
    start = request.form.get('start')
    end = request.form.get('end')
    if not (_isodate_ok(start) and _isodate_ok(end)):
        abort(400, 'invalid dates')
    db = get_db()
    db.execute('DELETE FROM taf_summary WHERE adate BETWEEN ? AND ?', (start, end))
    db.commit()
    flash(f'TAF removidos: {start} → {end}')
    return redirect(url_for('taf_view'))

# --- Run local ---
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
