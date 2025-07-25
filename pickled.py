#!/usr/bin/env python3
from flask import Flask, request, jsonify, render_template_string, send_from_directory, send_file, redirect, session
from flask_wtf.csrf import CSRFProtect, generate_csrf
from netmiko import ConnectHandler, NetMikoTimeoutException, NetMikoAuthenticationException
import io
from functools import wraps
from datetime import datetime
import time
import os
import glob
import json
import zipfile
import shutil
import csv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import atexit
import logging
import logging.handlers
from cryptography.fernet import Fernet
import base64
from werkzeug.utils import secure_filename
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import ipaddress
import re
from markupsafe import escape



__version__ = "1.0.6"

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))  # Chiave segreta per le sessioni
app.config['WTF_CSRF_ENABLED'] = True
app.config['WTF_CSRF_SECRET_KEY'] = os.environ.get('CSRF_SECRET_KEY', os.urandom(24).hex())
csrf = CSRFProtect(app)

# Credenziali hardcoded
USERNAME = os.environ.get('PICKLED_USERNAME', 'jar')
PASSWORD = os.environ.get('PICKLED_PASSWORD', 'cucumber')



logger = logging.getLogger(__name__)

# Percorsi dei file
current_dir = os.path.dirname(os.path.abspath(__file__))
SWITCHES_FILE = os.path.join(current_dir, 'switches.json')
SCHEDULES_FILE = os.path.join(current_dir, 'schedules.json')
KEY_FILE = os.path.join(current_dir, 'encryption.key')
LOG_DIR = os.path.join(current_dir, 'logs')
EVENTS_LOG = os.path.join(LOG_DIR, 'events.log')
BACKUP_DIR = os.path.join(current_dir, 'backups')

# Crea le directory necessarie se non esistono
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

# Crea il file di log se non esiste
if not os.path.exists(EVENTS_LOG):
    open(EVENTS_LOG, 'a').close()

# Configurazione logging
# Configurazione logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    handlers=[
        logging.handlers.TimedRotatingFileHandler(
            EVENTS_LOG, when='midnight', backupCount=7
        )
    ]
)

# Disabilita il logging di Werkzeug
logging.getLogger('werkzeug').setLevel(logging.WARNING)

# Crea le directory necessarie
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

# Configura il logger per i file
file_handler = logging.handlers.TimedRotatingFileHandler(
    EVENTS_LOG, when='midnight', interval=1, backupCount=12,
    encoding='utf-8', delay=False
)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
file_handler.suffix = "%d_%m_%Y.log"
logger.addHandler(file_handler)

# Inizializzazione dello scheduler
scheduler = BackgroundScheduler()
scheduler.start()
atexit.register(lambda: scheduler.shutdown())

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",  # O usare Redis in produzione
    strategy="fixed-window"
)



app.config.update(
    SESSION_COOKIE_SECURE=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=3600
)

#def validate_input(input_str, max_length=255, allowed_chars=None):
#    if not input_str or len(input_str) > max_length:
#        return False
#    if allowed_chars:
#        for char in input_str:
#            if char not in allowed_chars:
#                return False
#    return True

def validate_ip(ip):
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False

def validate_hostname(hostname):
    if len(hostname) > 255:
        return False
    if hostname[-1] == ".":
        hostname = hostname[:-1]
    allowed = re.compile("(?!-)[A-Z\d-]{1,63}(?<!-)$", re.IGNORECASE)
    return all(allowed.match(x) for x in hostname.split("."))

# Inizializzazione della crittografia
def get_encryption_key():
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, 'rb') as key_file:
            return key_file.read()
    else:
        key = Fernet.generate_key()
        with open(KEY_FILE, 'wb') as key_file:
            key_file.write(key)
        return key

fernet = Fernet(get_encryption_key())

def encrypt_password(password):
    if not password:
        return ""
    return fernet.encrypt(password.encode()).decode()

def decrypt_password(encrypted_password):
    if not encrypted_password:
        return ""
    return fernet.decrypt(encrypted_password.encode()).decode()

# Funzioni di persistenza dati
def load_switches():
    try:
        if not os.path.exists(SWITCHES_FILE):
            return []

        with open(SWITCHES_FILE, 'r', encoding='utf-8-sig') as f:
            content = f.read().strip()
            if not content:
                return []
            
            switches_data = json.loads(content)
            
            # Correzione: garantiamo che enable_password sia sempre valorizzato
            for switch in switches_data:
                if not switch.get('enable_password'):
                    switch['enable_password'] = switch['password']
            
            return switches_data
            
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in switches file: {str(e)}")
        return []
    except Exception as e:
        logger.error(f"Error loading switches: {str(e)}")
        return []

def save_switches(switches_data):
    with open(SWITCHES_FILE, 'w') as f:
        json.dump(switches_data, f, indent=4)

def load_schedules():
    try:
        if os.path.exists(SCHEDULES_FILE):
            with open(SCHEDULES_FILE, 'r') as f:
                schedules = json.load(f)
                for schedule in schedules:
                    if schedule.get('enabled', True):
                        add_scheduled_job(schedule)
                return schedules
        return []
    except (json.JSONDecodeError, FileNotFoundError) as e:
        logger.error(f"Error during schedule update: {str(e)}")
        return []

def save_schedules(schedules_data):
    with open(SCHEDULES_FILE, 'w') as f:
        json.dump(schedules_data, f, indent=4)

# Funzioni per lo scheduling
def add_scheduled_job(schedule):
    trigger = None
    schedule_type = schedule['type']
    time_str = schedule['time']
    hour, minute = map(int, time_str.split(':'))
    
    if schedule_type == 'once':
        run_date = datetime.strptime(schedule['date'], '%Y-%m-%d')
        run_date = run_date.replace(hour=hour, minute=minute)
        trigger = 'date'
        kwargs = {'run_date': run_date}
    elif schedule_type == 'daily':
        trigger = CronTrigger(hour=hour, minute=minute)
    elif schedule_type == 'weekly':
        day_of_week = schedule['day_of_week']
        trigger = CronTrigger(day_of_week=day_of_week, hour=hour, minute=minute)
    elif schedule_type == 'monthly':
        day = schedule['day']
        trigger = CronTrigger(day=day, hour=hour, minute=minute)
    elif schedule_type == 'yearly':
        month = schedule['month']
        day = schedule['day']
        trigger = CronTrigger(month=month, day=day, hour=hour, minute=minute)
    
    if trigger:
        job_func = execute_scheduled_backup if 'switch_index' in schedule else execute_global_scheduled_backup
        args = [schedule['switch_index']] if 'switch_index' in schedule else []
        
        scheduler.add_job(
            job_func,
            trigger,
            args=args,
            id=schedule['id'],
            name=f"Backup {'switch ' + str(schedule['switch_index']) if 'switch_index' in schedule else 'globale'}",
            replace_existing=True,
            **kwargs if schedule_type == 'once' else {}
        )

def execute_scheduled_backup(switch_index):
    try:
        with app.app_context():
            switches_data = load_switches()
            if 0 <= switch_index < len(switches_data):
                switch = switches_data[switch_index]
                logger.info(f"Esecuzione backup programmato per {switch['hostname']} ({switch['ip']})")
                result = backup_switch({'index': switch_index, 'scheduled': True})
                if not result.get('success', False):
                    logger.error(f"Error during scheduled backup: {result.get('message', 'Nessun dettaglio')}")
    except Exception as e:
        logger.error(f"Error during the execution of the scheduled backup: {str(e)}")

def execute_global_scheduled_backup():
    try:
        with app.app_context():
            logger.info("Global backup execution set for all devices")
            switches_data = load_switches()
            for i in range(len(switches_data)):
                execute_scheduled_backup(i)
    except Exception as e:
        logger.error(f"Error during the global backup execution: {str(e)}")

# Funzioni di utilità
def is_logged_in():
    return session.get('logged_in')

def login_required(f):
    @wraps(f)  # Importa wraps da functools
    @csrf.exempt
    def decorated_function(*args, **kwargs):
        if not is_logged_in():
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated_function

def get_schedule_description(schedule):
    desc = ''
    time_str = schedule.get('time', '00:00')
    
    if schedule['type'] == 'once':
        desc = f"Una volta il {schedule['date']} alle {time_str}"
    elif schedule['type'] == 'daily':
        desc = f"Giornaliero alle {time_str}"
    elif schedule['type'] == 'weekly':
        days = ['Domenica', 'Lunedì', 'Martedì', 'Mercoledì', 'Giovedì', 'Venerdì', 'Sabato']
        day = int(schedule['day_of_week'])
        desc = f"Settimanale ogni {days[day]} alle {time_str}"
    elif schedule['type'] == 'monthly':
        desc = f"Mensile il giorno {schedule['day']} alle {time_str}"
    elif schedule['type'] == 'yearly':
        months = ['Gennaio', 'Febbraio', 'Marzo', 'Aprile', 'Maggio', 'Giugno', 
                 'Luglio', 'Agosto', 'Settembre', 'Ottobre', 'Novembre', 'Dicembre']
        month = int(schedule['month']) - 1
        desc = f"Annuale il {schedule['day']} {months[month]} alle {time_str}"
    
    return desc

@app.after_request
def set_csrf_cookie(response):
    response.set_cookie('csrf_token', generate_csrf())
    return response

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute") 
def login():
    if request.method == 'POST':
        if request.form.get('username') == USERNAME and request.form.get('password') == PASSWORD:
            session['logged_in'] = True
            session.permanent = True
            # Assicurati che la sessione sia salvata prima del redirect
            session.modified = True
            return redirect('/')
        return '''
            <script>
                alert("Wrong credentials");
                window.location.href = "/login";
            </script>
        '''
    
    # Genera un nuovo token CSRF per il form
    csrf_token = generate_csrf()
    
    return f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>PICKLED - Login</title>
        <style>
            body {{ font-family: Arial; text-align: center; margin-top: 50px; }}
            input {{ padding: 8px; margin: 5px; width: 200px; }}
            button {{ padding: 10px 20px; background: #4CAF50; color: white; border: none; }}
        </style>
    </head>
    <body>
        <h2>PICKLED – Platform for Instant Config Keep & Lightweight Export Daemon</h2> <br/> <h3>Login</h3>
        <form method="post" action="/login">
            <input type="hidden" name="csrf_token" value="{csrf_token}">
            <div><input type="text" name="username" placeholder="Username" required></div>
            <div><input type="password" name="password" placeholder="Password" required></div>
            <button type="submit">Accedi</button>
        </form>
    </body>
    </html>
    '''

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect('/login')

@app.route('/')
@login_required
def index():
    current_date = datetime.now().strftime('%Y-%m-%d')
    return render_template_string(HTML_TEMPLATE, current_date=current_date)

def csrf_exempt_login_required(f):
    return csrf.exempt(login_required(f))

# API per la gestione degli switch
@app.route('/add_switch', methods=['POST'])
@login_required
def add_switch():
    data = request.get_json()
    if not all(key in data for key in ['hostname', 'ip', 'username', 'password']):
        return jsonify({'success': False, 'message': 'Missing data'}), 400
    
    # Validazione lunghezza massima
    max_length = 255
    if (len(data['hostname']) > max_length or 
        len(data['ip']) > max_length or 
        len(data['username']) > max_length):
        return jsonify({'success': False, 'message': 'Input too long'}), 400
    
    # Validazione caratteri permessi (solo alfanumerici e alcuni simboli)
    if not re.match(r'^[a-zA-Z0-9\-_\.]+$', data['hostname']):
        return jsonify({'success': False, 'message': 'Invalid characters in hostname'}), 400
        
    if not all(key in data for key in ['hostname', 'ip', 'username', 'password']):
        return jsonify({'success': False, 'message': 'Missing data'})
    if not validate_ip(data['ip']):
        return jsonify({'success': False, 'message': 'Invalid IP address'})
    if not validate_hostname(data['hostname']):
        return jsonify({'success': False, 'message': 'Invalid hostname'})  
          
    # Encrypt passwords
    encrypted_password = encrypt_password(data['password'])
    # Garantiamo che enable_password sia sempre uguale a password se non specificato
    encrypted_enable = encrypt_password(data.get('enable_password', data['password']))
    
    switch_data = {
        'hostname': data['hostname'],
        'ip': data['ip'],
        'username': data['username'],
        'password': encrypted_password,
        'enable_password': encrypted_enable,  # Questo campo non sarà mai vuoto
        'device_type': data.get('device_type', 'cisco_ios')
    }
    
    switches_data = load_switches()
    switches_data.append(switch_data)
    save_switches(switches_data)
    
    logger.info(f"Added device: {data['hostname']} ({data['ip']})")
    return jsonify({'success': True, 'message': 'Device added successfully'})

@app.route('/get_switches', methods=['GET'])
@login_required
def get_switches_api():
    switches_data = load_switches()
    return jsonify(switches_data)

@app.route('/update_switch', methods=['POST'])
@login_required
def update_switch():
    data = request.get_json()
    if 'index' not in data:
        return jsonify({'success': False, 'message': 'Indice mancante'})
    
    switches_data = load_switches()
    index = int(data['index'])
    
    if 0 <= index < len(switches_data):
        old_hostname = switches_data[index]['hostname']
        
        # Keep existing passwords if not provided, otherwise encrypt new ones
        password = encrypt_password(data['password']) if 'password' in data and data['password'] else switches_data[index]['password']
        # For enable_password, use the new password if provided, otherwise keep existing enable_password
        enable_password = encrypt_password(data['enable_password']) if 'enable_password' in data and data['enable_password'] else password
        
        switches_data[index] = {
            'hostname': data['hostname'],
            'ip': data['ip'],
            'username': data['username'],
            'password': password,
            'enable_password': enable_password,
            'device_type': data.get('device_type', switches_data[index].get('device_type', 'cisco_ios'))
        }
        
        save_switches(switches_data)
        logger.info(f"Aggiornato switch: da {old_hostname} a {data['hostname']} ({data['ip']})")
        return jsonify({'success': True, 'message': 'Switch aggiornato'})
    else:
        return jsonify({'success': False, 'message': 'Indice non valido'})

@app.route('/delete_switch', methods=['POST'])
@login_required
def delete_switch():
    data = request.get_json()
    if 'index' not in data:
        return jsonify({'success': False, 'message': 'Indice mancante'})
    
    switches_data = load_switches()
    index = int(data['index'])
    
    if 0 <= index < len(switches_data):
        deleted_switch = switches_data.pop(index)
        save_switches(switches_data)
        
        schedules_data = load_schedules()
        schedules_data = [s for s in schedules_data if s.get('switch_index') != index]
        save_schedules(schedules_data)
        
        logger.info(f"Eliminato switch: {deleted_switch['hostname']} ({deleted_switch['ip']})")
        return jsonify({'success': True, 'message': 'Switch eliminato'})
    else:
        return jsonify({'success': False, 'message': 'Indice switch non valido'})

# API per la gestione dei backup
@app.route('/backup_switch', methods=['POST'])
@login_required
def backup_switch_http():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'Invalid request data'})
            
        result = backup_switch(data)
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"Error in backup_switch_http: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'message': 'Internal server error',
            'error_type': 'ServerError'
        }), 500

@app.route('/delete_backup', methods=['POST'])
@login_required
def delete_backup():
    data = request.get_json()
    if 'filepath' not in data:
        return jsonify({'success': False, 'message': 'File path missing'}), 400

    try:
        filepath = data['filepath']
        # Normalizza il percorso per sicurezza
        filepath = os.path.normpath(filepath)
        backup_dir = os.path.abspath(BACKUP_DIR)
        requested_path = os.path.abspath(os.path.join(backup_dir, filepath))
        
        # Verifica che il percorso sia dentro la directory dei backup
        if not requested_path.startswith(backup_dir + os.sep):
            return jsonify({'success': False, 'message': 'Invalid file path'}), 403
        
        if os.path.isfile(requested_path):
            os.remove(requested_path)
            logger.info(f"Deleted backup file: {requested_path}")
            return jsonify({'success': True, 'message': 'Backup deleted'})
        else:
            return jsonify({'success': False, 'message': 'File not found'}), 404
            
    except Exception as e:
        logger.error(f"Error deleting backup: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500
        
def backup_switch(params: dict) -> dict:
    """
    Performs network switch configuration backup using multiple connection methods and retrieval techniques.
    Automatically handles different device types and fails safely on errors.

    Args:
        params: Dictionary containing parameters:
            - index (int): Switch index in the devices list
            - scheduled (bool, optional): Flag for scheduled backup. Default False.
            - retry_count (int, optional): Number of retry attempts. Default 2.

    Returns:
        dict: Operation result containing:
            - success (bool): Operation status
            - message (str): Descriptive message
            - hostname (str): Switch hostname
            - ip (str): Switch IP address
            - filename (str, optional): Backup filename if successful
            - error_type (str, optional): Error type if failed
            - output (str, optional): Partial output if available

    Raises:
        ValueError: If input parameters are invalid
    """
    # Initial parameter validation
    if not isinstance(params, dict) or 'index' not in params:
        raise ValueError("Missing or invalid parameters")

    logger.debug(f"Starting backup with params: { {k:v for k,v in params.items() if k not in ['password', 'secret']} }")

    try:
        # Validate switch index
        index = params.get('index')
        switches_data = load_switches()
        
        if not isinstance(switches_data, list):
            error_msg = "Invalid switches data format"
            logger.error(error_msg)
            return {
                'success': False,
                'message': error_msg,
                'error_type': 'DataError'
            }

        if index < 0 or index >= len(switches_data):
            error_msg = f"Invalid switch index {index}"
            logger.error(error_msg)
            return {
                'success': False,
                'message': error_msg,
                'error_type': 'IndexError'
            }

        # Extract device information
        switch = switches_data[index]
        hostname = switch['hostname']
        ip = switch['ip']
        username = switch['username']
        password = decrypt_password(switch['password'])
        enable_password = decrypt_password(switch['enable_password'])
        device_type = switch.get('device_type', 'cisco_ios')
        custom_command = switch.get('backup_command')

        # Prepare connection parameters
        device = {
            'device_type': device_type,
            'host': ip,
            'username': username,
            'password': password,
            'secret': enable_password,
            'timeout': 150,
            'session_timeout': 150,
            'global_delay_factor': 3,
            'fast_cli': False,
            'allow_auto_change': True,
            'verbose': False
        }

        logger.info(f"[{hostname}] Starting backup procedure")

        # Prepare backup file
        switch_folder = os.path.join(BACKUP_DIR, secure_filename(hostname))
        os.makedirs(switch_folder, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_filename = f"{hostname}_config_{timestamp}.txt"
        backup_path = os.path.join(switch_folder, backup_filename)

        # Attempt interactive backup method
        try:
            with ConnectHandler(**device) as net_connect:
                logger.info(f"[{hostname}] Connected, starting interactive backup")
                
                # Configure session
                net_connect.enable()
                net_connect.write_channel('\n')
                time.sleep(1)
                
                # Disable pagination
                pagination_commands = [
                    'terminal length 0\n',
                    'terminal width 512\n',
                    #'set cli screen-length 0\n'
                ]
                
                for cmd in pagination_commands:
                    net_connect.write_channel(cmd)
                    time.sleep(2)
                
                # Retrieve configuration
                full_output = ""
                config_commands = ['show running-config\n']
                
                for cmd in config_commands:
                    logger.info(f"[{hostname}] Executing: {cmd.strip()}")
                    net_connect.write_channel(cmd)
                    
                    start_time = time.time()
                    while time.time() - start_time < 60:
                        time.sleep(3)
                        new_data = net_connect.read_channel()
                        if new_data:
                            full_output += new_data
                            if any(pattern in new_data for pattern in ['#', 'end', '--More--']):
                                if '--More--' in new_data:
                                    net_connect.write_channel(' ')
                                break

                # Validate output
                if len(full_output.splitlines()) < 20:
                    raise Exception("Insufficient configuration data")

                # Clean and save configuration
                clean_output = "\n".join(
                    line for line in full_output.splitlines() 
                    if not any(line.startswith(cmd) for cmd in ['show', 'terminal', 'enable', 'conf t', 'exit'])
                )
                
                with open(backup_path, 'w', encoding='utf-8') as f:
                    f.write(clean_output)

                logger.info(f"[{hostname}] Backup completed successfully")
                return {
                    'success': True,
                    'message': "Backup completed",
                    'hostname': hostname,
                    'ip': ip,
                    'filename': backup_filename
                }

        except Exception as e:
            logger.warning(f"[{hostname}] Interactive method failed, trying simple method: {str(e)}")
            
            # Fallback to simple method
            try:
                with ConnectHandler(**device) as net_connect:
                    output = net_connect.send_command_timing(
                        custom_command or 'show running-config',
                        delay_factor=5,
                        max_loops=3000
                    )
                    
                    if not output or len(output.splitlines()) < 10:
                        raise Exception("Insufficient output")
                    
                    with open(backup_path, 'w', encoding='utf-8') as f:
                        f.write(output)

                    logger.info(f"[{hostname}] Backup completed with simple method")
                    return {
                        'success': True,
                        'message': "Backup completed with fallback method",
                        'hostname': hostname,
                        'ip': ip,
                        'filename': backup_filename
                    }

            except Exception as fallback_error:
                error_msg = f"All backup methods failed: {str(fallback_error)}"
                logger.error(f"[{hostname}] {error_msg}")
                return {
                    'success': False,
                    'message': error_msg,
                    'hostname': hostname,
                    'ip': ip,
                    'error_type': 'BackupError',
                    'output': output[:1000] if 'output' in locals() else None
                }

    except (NetMikoTimeoutException, NetMikoAuthenticationException) as e:
        error_msg = f"Connection error: {str(e)}"
        logger.error(f"[{hostname}] {error_msg}")
        return {
            'success': False,
            'message': error_msg,
            'hostname': hostname,
            'ip': ip,
            'error_type': type(e).__name__
        }

    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"
        logger.error(f"[{hostname}] {error_msg}")
        return {
            'success': False,
            'message': error_msg,
            'hostname': hostname if 'hostname' in locals() else 'unknown',
            'ip': ip if 'ip' in locals() else 'unknown',
            'error_type': 'UnexpectedError'
        }



@app.route('/backup_all_switches', methods=['POST'])
@login_required
def backup_all_switches():
    try:
        logger.info("Starting backup process for all devices")
        
        # Carica gli switch con controllo errori rinforzato
        try:
            switches_data = load_switches()
            if not isinstance(switches_data, list):
                error_msg = "Invalid switches data format"
                logger.error(error_msg)
                return jsonify({
                    'success': False,
                    'message': error_msg,
                    'count': 0,
                    'total': 0,
                    'results': []
                })
        except Exception as e:
            error_msg = f"Failed to load switches: {str(e)}"
            logger.error(error_msg)
            return jsonify({
                'success': False,
                'message': error_msg,
                'count': 0,
                'total': 0,
                'results': []
            })

        if not switches_data:
            logger.warning("No devices configured for backup")
            return jsonify({
                'success': False,
                'message': 'No devices configured',
                'count': 0,
                'total': 0,
                'results': []
            })

        results = []
        for i, switch in enumerate(switches_data, 1):
            progress_msg = f"Processing device {i}/{len(switches_data)}: {switch['hostname']}"
            logger.info(progress_msg)
            
            result = backup_switch({'index': i-1})
            results.append({
                'success': result.get('success', False),
                'hostname': switch['hostname'],
                'ip': switch['ip'],
                'message': result.get('message', ''),
                'filename': result.get('filename', '')
            })

        success_count = sum(1 for r in results if r['success'])
        completion_msg = f"Backup completed. Success: {success_count}/{len(switches_data)}"
        logger.info(completion_msg)
        
        return jsonify({
            'success': True,
            'count': success_count,
            'total': len(switches_data),
            'results': results
        })

    except Exception as e:
        error_msg = f"Unexpected error in backup_all_switches: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return jsonify({
            'success': False,
            'message': error_msg,
            'count': 0,
            'total': 0,
            'results': []
        }), 500

@app.route('/get_switch_backups', methods=['POST'])
@login_required
def get_switch_backups():
    data = request.get_json()
    if 'index' not in data:
        return jsonify({'success': False, 'message': 'Indice mancante'})
    
    switches_data = load_switches()
    index = int(data['index'])
    
    if index < 0 or index >= len(switches_data):
        return jsonify({'success': False, 'message': 'Indice switch non valido'})
    
    switch = switches_data[index]
    hostname = switch['hostname']
    switch_folder = os.path.join(BACKUP_DIR, secure_filename(hostname))
    
    if not os.path.exists(switch_folder):
        return jsonify({'success': True, 'hostname': hostname, 'backups': []})
    
    backups = []
    for filename in sorted(os.listdir(switch_folder), reverse=True):
        if filename.endswith('.txt'):
            filepath = os.path.join(switch_folder, filename)
            backups.append({
                'filename': filename,
                'path': filepath
            })
    
    return jsonify({
        'success': True,
        'hostname': hostname,
        'backups': backups
    })

@app.route('/get_backup_content', methods=['POST'])
@login_required
def get_backup_content():
    # Validazione dell'input JSON
    if not request.is_json:
        return jsonify({'success': False, 'message': 'Request must be JSON'}), 400
    
    data = request.get_json()
    if 'filepath' not in data:
        return jsonify({'success': False, 'message': 'Missing file path'}), 400
    
    # Validazione del percorso file
    try:
        filepath = data['filepath']
        if not isinstance(filepath, str):
            raise ValueError("Filepath must be a string")
            
        # Normalizzazione del percorso per prevenire directory traversal
        filepath = os.path.normpath(filepath)
        backup_dir = os.path.abspath(BACKUP_DIR)
        requested_path = os.path.abspath(os.path.join(backup_dir, filepath))
        
        # Verifica che il percorso sia dentro la directory dei backup
        if not requested_path.startswith(backup_dir + os.sep):
            return jsonify({'success': False, 'message': 'Invalid file path'}), 403
        
        # Verifica esistenza file
        if not os.path.isfile(requested_path):
            return jsonify({'success': False, 'message': 'File not found'}), 404
        
        # Lettura e sanitizzazione del contenuto
        with open(requested_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Sanitizzazione del contenuto per prevenire XSS
        sanitized_content = escape(content)
        
        return jsonify({
            'success': True,
            'filename': os.path.basename(requested_path),
            'content': sanitized_content  # Contenuto sanitizzato
        })
        
    except UnicodeDecodeError:
        return jsonify({'success': False, 'message': 'Invalid file encoding (UTF-8 required)'}), 400
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400
    except Exception as e:
        logging.error(f"Error accessing backup file: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'message': 'Internal server error'}), 500

# API per la gestione degli schedule
@app.route('/add_schedule', methods=['POST'])
@login_required
def add_schedule():
    data = request.get_json()
    if not all(key in data for key in ['type', 'time']):
        return jsonify({'success': False, 'message': 'Dati mancanti'})
    
    schedule_id = f"sch_{int(time.time())}_{len(load_schedules())}"
    data['id'] = schedule_id
    data['created_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    schedules_data = load_schedules()
    schedules_data.append(data)
    save_schedules(schedules_data)
    add_scheduled_job(data)
    
    logger.info(f"New schedule added: {get_schedule_description(data)}")
    return jsonify({
        'success': True,
        'message': 'Schedule added',
        'id': schedule_id
    })

@app.route('/get_schedules', methods=['GET'])
@login_required
def get_schedules():
    schedules_data = load_schedules()
    jobs = scheduler.get_jobs()
    
    for schedule in schedules_data:
        job = next((j for j in jobs if j.id == schedule['id']), None)
        if job:
            schedule['next_run'] = job.next_run_time.strftime('%Y-%m-%d %H:%M:%S') if job.next_run_time else "N/A"
            schedule['enabled'] = True
        else:
            schedule['next_run'] = "N/A"
            schedule['enabled'] = False
    
    return jsonify(schedules_data)

@app.route('/toggle_schedule', methods=['POST'])
@login_required
def toggle_schedule():
    data = request.get_json()
    if not all(key in data for key in ['id', 'enabled']):
        return jsonify({'success': False, 'message': 'Dati mancanti'})
    
    schedules_data = load_schedules()
    schedule = next((s for s in schedules_data if s['id'] == data['id']), None)
    
    if not schedule:
        return jsonify({'success': False, 'message': 'Pianificazione non trovata'})
    
    if data['enabled']:
        add_scheduled_job(schedule)
        schedule['enabled'] = True
        message = 'Pianificazione attivata'
    else:
        scheduler.remove_job(data['id'])
        schedule['enabled'] = False
        message = 'Pianificazione disattivata'
    
    save_schedules(schedules_data)
    logger.info(f"Pianificazione {data['id']} {'attivata' if data['enabled'] else 'disattivata'}")
    return jsonify({'success': True, 'message': message})

@app.route('/delete_schedule', methods=['POST'])
@login_required
def delete_schedule():
    data = request.get_json()
    if 'id' not in data:
        return jsonify({'success': False, 'message': 'ID mancante'})
    
    schedules_data = load_schedules()
    schedule = next((s for s in schedules_data if s['id'] == data['id']), None)
    
    if not schedule:
        return jsonify({'success': False, 'message': 'Pianificazione non trovata'})
    
    scheduler.remove_job(data['id'])
    schedules_data = [s for s in schedules_data if s['id'] != data['id']]
    save_schedules(schedules_data)
    
    logger.info(f"Eliminata pianificazione: {get_schedule_description(schedule)}")
    return jsonify({'success': True, 'message': 'Pianificazione eliminata'})

# API per la gestione dei log
@app.route('/log_event', methods=['POST'])
@login_required
def log_event():
    data = request.get_json()
    if 'message' in data:
        logger.info(data['message'])
    return jsonify({'success': True})

@app.route('/get_full_log', methods=['GET'])
@login_required
def get_full_log():
    try:
        log_content = ""
        if os.path.exists(EVENTS_LOG):
            with open(EVENTS_LOG, 'r') as f:
                # Filtra solo le righe che iniziano con timestamp e non contengono werkzeug
                log_content = "\n".join(
                    line for line in f.readlines() 
                    if line.strip() and not "werkzeug" in line
                )
        
        archived_logs = []
        if os.path.exists(LOG_DIR):
            for filename in sorted(os.listdir(LOG_DIR), reverse=True):
                if filename.startswith('events.') and filename.endswith('.log') and filename != 'events.log':
                    with open(os.path.join(LOG_DIR, filename), 'r') as f:
                        # Filtra anche i log archiviati
                        content = "\n".join(
                            line for line in f.readlines()
                            if line.strip() and not "werkzeug" in line
                        )
                        if content:
                            archived_logs.append(f"=== Log {filename} ===\n{content}\n")
        
        full_log = log_content + "\n".join(archived_logs)
        return jsonify({'success': True, 'log': escape(full_log)})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

# API per l'import/export CSV
@app.route('/upload_csv', methods=['POST'])
@login_required
def upload_csv():
    if 'csv_file' not in request.files:
        return jsonify({'success': False, 'message': 'No file provided'})
    
    csv_file = request.files['csv_file']
    if csv_file.filename == '':
        return jsonify({'success': False, 'message': 'No file selected'})
    
    if not csv_file.filename.endswith('.csv'):
        return jsonify({'success': False, 'message': 'File must be .csv'})
    
    try:
        stream = io.StringIO(csv_file.stream.read().decode('UTF-8'))
        csv_reader = csv.DictReader(stream)
        
        switches_data = load_switches()
        added = 0
        skipped = 0
        existing_ips = {sw['ip'] for sw in switches_data}
        
        for row in csv_reader:
            if not all(field in row for field in ['hostname', 'ip', 'username', 'password']):
                return jsonify({'success': False, 'message': 'CSV format not valid'})
            
            if row['ip'] in existing_ips:
                skipped += 1
                continue
            
            # Always set enable_password equal to password if not provided in CSV
            encrypted_password = encrypt_password(row['password'])
            encrypted_enable_password = encrypt_password(row.get('enable_password', row['password']))
            
            switches_data.append({
                'hostname': row['hostname'],
                'ip': row['ip'],
                'username': row['username'],
                'password': encrypted_password,
                'enable_password': encrypted_enable_password,
                'device_type': row.get('device_type', 'cisco_ios')
            })
            existing_ips.add(row['ip'])
            added += 1
        
        if added > 0:
            save_switches(switches_data)
            logger.info(f"Loaded {added} devices from CSV, {skipped} already present")
        
        return jsonify({
            'success': True,
            'message': 'CSV loaded successfully',
            'added': added,
            'skipped': skipped
        })
    except Exception as e:
        logger.error(f"Error while loading CSV: {str(e)}")
        return jsonify({'success': False, 'message': str(e)})
        
@app.route('/export_switches_csv', methods=['GET'])
@login_required
def export_switches_csv():
    try:
        switches_data = load_switches()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['hostname', 'ip', 'username'])
        
        for switch in switches_data:
            writer.writerow([switch['hostname'], switch['ip'], switch['username']])
        
        output.seek(0)
        mem_file = io.BytesIO()
        mem_file.write(output.getvalue().encode('utf-8'))
        mem_file.seek(0)
        
        logger.info(f"Exported {len(switches_data)} devices in CSV")
        return send_file(
            mem_file,
            mimetype='text/csv',
            as_attachment=True,
            download_name=f'switches_backup_{datetime.now().strftime("%Y%m%d")}.csv'
        )
    except Exception as e:
        logger.error(f"Error during the export of CSV: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500





# HTML Template (rimasto invariato)
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PICKLED – Because broken routers don’t explain themselves</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/select2/4.0.13/css/select2.min.css">
    <style>
        :root {
            --primary-color: #3498db;
            --primary-hover: #2980b9;
            --success-color: #2ecc71;
            --success-hover: #27ae60;
            --danger-color: #e74c3c;
            --danger-hover: #c0392b;
            --warning-color: #f39c12;
            --warning-hover: #e67e22;
            --purple-color: #9b59b6;
            --purple-hover: #8e44ad;
            --dark-color: #2c3e50;
            --light-color: #ecf0f1;
            --gray-light: #f5f5f5;
            --gray-medium: #ddd;
            --gray-dark: #333;
        }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: var(--gray-light);
        }
        
	/* Stile per la cella dell'header */
	.switch-table th:nth-child(4) {
	    position: relative;  /* Contesto per posizionamento assoluto */
	    padding-right: 120px;  /* Spazio per il selettore */
	}

	/* Stile per il contenitore del selettore */
	.per-page-selector {
	    position: absolute;
	    right: 10px;  
	    top: 50%;
	    transform: translateY(-50%);
	}

	/* Stile per il select */
	.per-page-selector select {
	    padding: 3px;
	    border-radius: 3px;
	    border: 1px solid #ddd;
	    background-color: white;
	    font-size: 12px;
	    width: auto;  /* Larghezza automatica in base al contenuto */
	}

     .log {
         margin-top: 30px;
         max-height: 300px;
         overflow-y: auto;
         background-color: #1a1a1a;
         color: #e0e0e0;
         padding: 15px;
         border-radius: 4px;
         font-family: monospace;
         white-space: pre-wrap;
         font-size: 14px;
         line-height: 1.5;
     }
     .log div {
         margin-bottom: 5px;
         padding: 3px 5px;
         border-radius: 3px;
     }
     .log div:hover {
         background-color: #2a2a2a;
     }
        .app-container {
            display: flex;
            min-height: calc(100vh - 40px);
            gap: 20px;
        }
	/* Tooltip */
	.copy-tooltip {
	    position: relative;
	    display: block;
	    cursor: pointer;
	    margin-top: 20px; /* Spazio per il tooltip */
	}

	    .copy-tooltip .tooltiptext {
		visibility: hidden;
		width: 140px;
		background-color: #333;
		color: #fff;
		text-align: center;
		border-radius: 4px;
		padding: 5px;
		position: absolute;
		z-index: 1001;  
		bottom: calc(100% - 10px);  
		left: 50%;
		transform: translateX(-50%);
		opacity: 0;
		transition: opacity 0.3s;
		font-size: 13px;
		font-family: Arial, sans-serif;
		pointer-events: none;
	    }

	.copy-tooltip:hover .tooltiptext {
	    visibility: visible;
	    opacity: 1;
	}

	.copy-tooltip.copied .tooltiptext {
	    background-color: #4CAF50;
	}

	.config-content {
	    font-family: monospace;
	    white-space: pre;
	    background-color: #f8f8f8;
	    padding: 15px;
	    border-radius: 4px;
	    max-height: 70vh;
	    overflow-y: auto;
	    margin: 0;
	    border: 1px solid #ddd;
	    position: relative;
	}
        .left-panel {
            width: 300px;
            background-color: white;
            padding: 15px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
        }
        .left-panel > button {
            width: calc(100% - 24px);
            padding: 12px;
            margin-top: 5px;
        }
        /* Colonna centrale - Tabella switch */
	.center-panel {
	    flex: 1;
	    background-color: white;
	    padding: 20px;
	    border-radius: 8px;
	    box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
	    display: flex;
	    flex-direction: column;
	    margin: 0 20px; /* Aggiunge spazio tra le colonne */
	}
        /* Colonna destra - Scheduler e log */
	.right-panel {
	    width: 350px;
	    background-color: white;
	    padding: 15px;
	    border-radius: 8px;
	    box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
	    display: flex;
	    flex-direction: column;
	}
        /* Tabella switch con scroll */
	.switch-table-container {
	    flex: 1;
	    overflow-y: auto;
	    margin-top: 10px;
	}
        /* Barra di ricerca */
        .search-container {
            display: flex;
            margin-bottom: 15px;
        }
        .search-container input {
            flex: 1;
            padding: 10px;
            border: 1px solid var(--gray-medium);
            border-radius: 4px;
        }      
        /* Paginazione */
	.pagination {
	    display: flex;
	    justify-content: center;
	    align-items: center;
	    margin-top: 15px;
	    gap: 5px;
	    flex-wrap: wrap;
	}

	.pagination button {
	    padding: 5px 10px;
	    background-color: var(--gray-light);
	    color: var(--dark-color);
	    border: 1px solid var(--gray-medium);
	    border-radius: 4px;
	    cursor: pointer;
	    min-width: 32px;
	    transition: all 0.2s;
	}
	.pagination button:hover:not(:disabled) {
	    background-color: var(--primary-color);
	    color: white;
	    border-color: var(--primary-color);
	}
	.pagination button.active {
	    background-color: var(--primary-color);
	    color: white;
	    border-color: var(--primary-color);
	}
	.pagination button:disabled {
	    opacity: 0.5;
	    cursor: not-allowed;
	}

	.pagination button i {
	    font-size: 14px;
	}
        h1 {
            color: var(--dark-color);
            text-align: center;
            margin-bottom: 30px;
        }
        h2 {
            color: var(--dark-color);
            border-bottom: 2px solid var(--primary-color);
            padding-bottom: 8px;
            margin-top: 0;
        }
        .form-group input {
            width: calc(100% - 24px);
            padding: 10px 12px;
            border: 1px solid var(--gray-medium);
            border-radius: 6px;
            font-size: 16px;
            transition: border-color 0.3s, box-shadow 0.3s;
        }
        .form-group input:focus {
            border-color: var(--primary-color);
            box-shadow: 0 0 0 2px rgba(52, 152, 219, 0.2);
            outline: none;
        }
        .form-group {
            margin-bottom: 10px;
        }
        .form-group label {
            display: block;
            margin-bottom: 5px;
            font-weight: 600;
            color: var(--dark-color);
            font-size: 13px;
        }
        label {
            display: block;
            margin-bottom: 5px;
            font-weight: 600;
        }
        input {
            width: 100%;
            padding: 10px;
            border: 1px solid var(--gray-medium);
            border-radius: 4px;
            font-size: 16px;
        }
        button {
            background-color: var(--primary-color);
            color: white;
            border: none;
            padding: 12px 20px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 16px;
            transition: background-color 0.3s;
        }
        button:hover {
            background-color: var(--primary-hover);
        }
        .status-container {
            position: fixed;
            top: 20px;
            right: 20px;
            z-index: 1000;
            max-width: 400px;
        }
        .status {
            padding: 15px;
            border-radius: 4px;
            margin-bottom: 10px;
            display: none;
            box-shadow: 0 2px 10px rgba(0,0,0,0.2);
        }
        .success {
            background-color: #d4edda;
            color: #155724;
            border: 1px solid #c3e6cb;
        }
        .error {
            background-color: #f8d7da;
            color: #721c24;
            border: 1px solid #f5c6cb;
        }
        .log {
            margin-top: 30px;
            max-height: 300px;
            overflow-y: auto;
            background-color: var(--dark-color);
            color: var(--light-color);
            padding: 15px;
            border-radius: 4px;
            font-family: monospace;
            white-space: pre-wrap;
        }
        /* Log attività ridimensionato */
        .log-panel {
            flex: 1;
            overflow-y: auto;
            margin-top: 20px;
            background-color: #1a1a1a;
            color: #e0e0e0;
            padding: 15px;
            border-radius: 4px;
            font-family: monospace;
        }
	.switch-table {
	    width: 100%;
	    border-collapse: separate;  /* Cambiato da collapse a separate */
	    border-spacing: 0;
	    margin-top: 20px;
	    border-radius: 8px;  /* Aggiunto per smussare gli angoli */
	    overflow: hidden;  /* Per mantenere i bordi arrotondati */
	    box-shadow: 0 2px 10px rgba(0,0,0,0.1);  /* Aggiunto ombreggiatura per migliorare l'aspetto */
	}
        .switch-table th, .switch-table td {
            padding: 8px 12px;
            text-align: left;
            border-bottom: 1px solid var(--gray-medium);
        }
        .switch-table th {
            background-color: var(--primary-color);
            color: white;
            cursor: pointer;
            user-select: none;
            position: sticky;
            top: 0;
        }
        .switch-table th:hover {
            background-color: var(--primary-hover);
        }
        .switch-table tr {
            line-height: 1.3;
        }
        .switch-table tr:hover {
            background-color: var(--gray-light);
        }
	.search-header-container {
	    margin-bottom: 5px; /* Riduci da 15px a 10px */
	}
	.search-button {
	    padding: 4px 8px;
	    height: 30px; /* Altezza fissa */
	    display: flex;
	    align-items: center;
	    justify-content: center;
	    gap: 5px;
	    border-radius: 4px;
	    font-size: 13px;
	    cursor: pointer;
	    transition: all 0.2s;
	    white-space: nowrap;
	}
        .search-button i {
            font-size: 12px;  /* Ridotto da 13px */
        }
	.search-buttons {
	    padding: 8px 12px;
	    min-width: 100px;  /* Larghezza minima per uniformità */
	    height: 36px;      /* Altezza fissa */
	    display: flex;
	    align-items: center;
	    justify-content: center;
	    gap: 5px;
	}
	.search-buttons-container {
	    display: flex;
	    gap: 8px; /* Riduci lo spazio tra i pulsanti */
	    align-items: center;
	}
        .action-btn {
            padding: 6px 10px;
            margin: 0 2px;
            font-size: 14px;
            min-width: 30px;
            transition: all 0.2s ease;
        }
        .action-btn:hover {
  	    transform: translateY(-1px);
     	    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
	}
	.search-buttons:hover {
	    opacity: 0.9;
	    transform: translateY(-1px);
	}
        .edit-btn {
            background-color: var(--warning-color);
        }
        .edit-btn:hover {
            background-color: var(--warning-hover);
        }
        .delete-btn {
            background-color: var(--danger-color);
        }
        .delete-btn:hover {
            background-color: var(--danger-hover);
        }
        .backup-btn {
            background-color: var(--success-color);
        }
        .backup-btn:hover {
            background-color: var(--success-hover);
        }
        .view-btn {
            background-color: var(--purple-color);
        }
        .view-btn:hover {
            background-color: var(--purple-hover);
        }
        .backup-all-btn {
            background-color: var(--purple-color);
            margin-top: 20px;
            width: 100%;
        }
        .backup-all-btn:hover {
            background-color: var(--purple-hover);
        }

	.exp-btn {
	    background-color: #2c3e50;
	    color: white;
	}

	.backup-btn {
	    background-color: #2ecc71;
	    color: white;
	}

	.search-button:hover {
	    opacity: 0.9;
	    transform: translateY(-1px);
	}
        .backup-exp-btn {
            background-color: var(--blue-color);
            margin-top: 20px;
            width: 100%;
        }
        .backup-exp-btn:jover {
            background-color: var(--blue-hover);
        }
        .csv-btn {
            background-color: var(--dark-color);
            margin-top: 10px;
            width: 100%;
        }
        .csv-btn:hover {
            background-color: #680cec;
        }
	    .modal {
		display: none;
		position: fixed;
		z-index: 1050;  /* Aumentato per sicurezza */
		left: 0;
		top: 0;
		width: 100%;
		height: 100%;
		background-color: rgba(0,0,0,0.5);
		overflow: auto;
	    }
        .modal-content {
            background-color: white;
            margin: 5% auto;
            padding: 20px;
            border-radius: 8px;
            width: 80%;
            max-width: 900px;
            max-height: 80vh;
            display: flex;
            flex-direction: column;
            overflow: auto;
	    transition: all 0.3s ease;
        }
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        .modal-title {
            font-size: 1.5em;
            font-weight: bold;
        }
        .close-btn {
            font-size: 1.5em;
            cursor: pointer;
        }
	.modal-body {
	    display: flex;
	    min-height: 0;
	    flex: 1; 
	    gap: 15px;
	    padding: 10px;
	    min-height: 0; 
            overflow: auto;  /* Aggiunto */
	}
	.config-container {
	    flex: 1;
	    display: flex;
	    flex-direction: column;
	    min-height: 0;
	    position: relative;
	    overflow: auto;
	    padding-right: 15px;
	}
	.backup-list {
	    width: 20%;
	    overflow-y: auto;
	    min-width: 300px; /* Larghezza minima */
	    max-height: 100%;
	}

	.backup-content {
	    width: 70%;
	    display: flex;
	    flex-direction: column;
	    min-height: 0
	    overflow: auto;
	}
	/* Placeholder */
	#backup-content-placeholder {
	    display: flex;
	    flex-direction: column;
	    align-items: center;
	    justify-content: center;
	    height: 100%;
	    color: #777;
	}
	#backup-content {
 	   background: #f8f8f8;
   	   border-radius: 4px;
	   padding: 12px;
	}
        .backup-item {
            padding: 10px;
            margin-bottom: 5px;
            border-radius: 4px;
            cursor: pointer;
        }
     
        .backup-item:hover {
            background-color: var(--gray-light);
        }
        .backup-item.active {
            background-color: var(--primary-color);
            color: white;
        }
        .config-content {
	    font-family: monospace;
	    white-space: pre;
	    background-color: #f8f8f8;
	    padding: 15px;
	    border-radius: 4px;
	    overflow-y: auto;
	    flex: 1;
	    border: 1px solid #ddd;
	    margin: 0;
	    max-height: 100%;
        }
        .split-view {
            display: flex;
            width: 100%;
        }
        .form-column {
            flex: 1;
            padding: 0 10px;
        }
        .log-modal-content {
            background-color: white;
            margin: 5% auto;
            padding: 20px;
            border-radius: 8px;
            width: 80%;
            max-width: 900px;
            max-height: 80vh;
        }
	.log-content {
	    font-family: monospace;
	    white-space: pre-wrap;
	    background-color: #f8f8f8;
	    padding: 15px;
	    border-radius: 4px;
	    max-height: 60vh; /* Aumenta l'altezza massima */
	    overflow-y: auto;
	    width: calc(100% + 20px); /* Compensa i margini negativi */
	}
	.log-line {
	    margin: 0;
	    padding: 2px 5px;
	    border-radius: 2px;
	    transition: background-color 0.2s;
	}

	.log-line:hover {
	    background-color:  #e7f585; 
	    cursor: default;
	}
        ::-webkit-scrollbar {
            width: 8px;
            height: 8px;
        }
        ::-webkit-scrollbar-track {
            background: #f1f1f1;
        }
        ::-webkit-scrollbar-thumb {
            background: #888;
            border-radius: 4px;
        }
        ::-webkit-scrollbar-thumb:hover {
            background: #555;
        }
        .file-input-container {
            margin-top: 15px;
        }
        .file-input-label {
            display: block;
            margin-bottom: 5px;
            font-weight: 600;
        }
        .file-input {
            width: calc(100% - 24px);
            padding: 8px 12px;
        }
        /* Stili per lo scheduling */

        .schedule-form {
            background-color: var(--gray-light);
            padding: 12px;
            border-radius: 6px;
            margin-bottom: 15px;
        }
        .schedule-form h3 {
            margin-top: 0;
            color: var(--dark-color);
            border-bottom: 1px solid var(--gray-medium);
            padding-bottom: 8px;
        }
        .schedule-option {
            display: none;
            margin-top: 10px;
        }
        .schedule-option.active {
            display: block;
        }
        .schedule-list {
            margin-top: 15px;
        }
        .schedule-item {
            padding: 10px;
            background-color: white;
            border: 1px solid var(--gray-medium);
            border-radius: 4px;
            margin-bottom: 10px;
        }
        .schedule-item-header {
            display: flex;
            justify-content: space-between;
            font-weight: bold;
        }
        .schedule-item-actions {
            display: flex;
            gap: 5px;
        }
        .schedule-item-actions button {
            padding: 2px 6px;
            font-size: 12px;
        }
        .select2-container {
            width: 100% !important;
            margin-bottom: 15px;
        }
	#search-input {
	    height: 30px;
	    padding: 5px 30px 5px 10px;
	    border: 1px solid #ddd;
	    border-radius: 4px;
	    margin-right: 10px;
	    transition: all 0.3s ease;
	    width: 180px; /* Larghezza iniziale */
	}

	#search-input:focus {
	    width: 250px;
	    border-color: var(--primary-color);
	    outline: none;
	    box-shadow: 0 0 0 2px rgba(52, 152, 219, 0.2);
	}
        /* Riduci dimensioni icona di ricerca */
        #search-input + .fa-search {
            font-size: 12px;  /* Ridotto da 14px */
            right: 8px;  /* Aggiustato posizione */
            top: 9px;  /* Aggiustato posizione */
        }
        input:not([type="file"]):not([type="checkbox"]):not([type="radio"]),
        select {
            height: 32px;  /* Ridotto da altezza precedente */
            padding: 5px 10px;  /* Ridotto da 8px 12px */
            font-size: 14px;  /* Ridotto da 16px */
        }        
    </style>
</head>
<body>
    <div class="status-container">
        <div id="status-message" class="status"></div>
    </div>
    <div class="app-container">
        <!-- Pannello sinistro - Form aggiunta switch -->
        <div class="left-panel">
            <h2>Add device</h2>
            <div class="form-group">
                <label for="hostname">Hostname:</label>
                <input type="text" id="hostname" placeholder="Es: Switch1">
            </div>
            <div class="form-group">
                <label for="ip">IP Address:</label>
                <input type="text" id="ip" placeholder="Es: 192.168.1.1">
            </div>
            <div class="form-group">
                <label for="username">Username:</label>
                <input type="text" id="username" placeholder="Username SSH">
            </div>
            <div class="form-group">
                <label for="password">Password:</label>
                <input type="password" id="password" placeholder="Password SSH">
            </div>
    	    <div class="form-group">
	        <label for="enable-password">Enable Password (optional):</label>
	        <input type="password" id="enable-password" placeholder="Enable/Secret password">
	    </div>
	   
            <div class="form-group">
	    <label for="device-type">Tipo dispositivo:</label>
	    <select id="device-type" class="form-control">
		<option value="cisco_ios">Cisco IOS</option>
		<option value="cisco_xe">Cisco IOS-XE</option>
		<option value="cisco_xr">Cisco IOS-XR</option>
		<option value="huawei">Huawei</option>
		<option value="juniper">Juniper JunOS</option>
	    </select> 
	    </div>

            <button onclick="addSwitch()">Add Device</button>

            
            <h2 style="margin-top: 30px;">Load CSV</h2>
            <div class="file-input-container">
                <label class="file-input-label" for="csv-file">Select CSV file:</label>
                <input type="file" id="csv-file" class="file-input" accept=".csv">
            </div>
            <button class="csv-btn" onclick="uploadCSV()">
                <i class="fas fa-file-import"></i> Load CSV
            </button>

	</div>


	    <!-- Colonna centrale - Tabella switch -->
	    <div class="center-panel">
		<!-- Header con titolo, ricerca e pulsanti -->
		<div class="search-header-container">
		    <div style="display: flex; justify-content: space-between; align-items: center;">
			<h2 style="margin: 0;">Device List</h2>
			<div class="search-buttons-container">
			    <div style="position: relative;">
			    <input type="text" id="search-input" placeholder="Search devices..." 
				   style="padding: 5px 30px 5px 10px; border: 1px solid #ddd; border-radius: 4px; height: 30px; margin-right: 10px;"
				   oninput="filterSwitches()">
   				 <i class="fas fa-search" style="position: absolute; right: 15px; top: 50%; transform: translateY(-50%); color: #777;"></i>
			    </div>
			    <button class="search-button exp-btn" onclick="exportSwitchesToCSV()">
				<i class="fas fa-file-export"></i> Export
			    </button>
			    <button class="search-button backup-btn" onclick="backupAllSwitches()">
				<i class="fas fa-download"></i> Backup
			    </button>
			    <button class="search-button" onclick="window.location.href='/logout'" style="background-color: #e74c3c;">
				<i class="fas fa-sign-out-alt"></i> Logout
			    </button>
			</div>
		    </div>
		</div>
		
		<!-- Tabella con scroll -->
		<div class="switch-table-container">
		    <table class="switch-table">
			    <thead>
				<tr>
				    <th onclick="sortTable(0)">Hostname <i class="fas fa-sort"></i></th>
				    <th onclick="sortTable(1)">IP <i class="fas fa-sort"></i></th>
				    <th onclick="sortTable(2)">Username <i class="fas fa-sort"></i></th>
					<th>Actions
					    <span class="per-page-selector">
						<select id="items-per-page" onchange="changeItemsPerPage()">
						    <option value="5">5</option>
						    <option value="10">10</option>
						    <option value="15" selected>15</option>
						    <option value="20">20</option>
						    <option value="50">50</option>
						    <option value="100">100</option>
						</select>
					    </span>
					</th>

				</tr>
			    </thead>
		        <tbody id="switches-table-body">
		            <!-- Le righe della tabella verranno aggiunte qui dinamicamente -->
		        </tbody>
		    </table>
		</div>
		
		<!-- Paginazione -->
		<div class="pagination" id="pagination">
		    <!-- Popolato dinamicamente da JavaScript -->
		</div>
	    </div>

        <!-- Colonna destra - Scheduler e log -->
        <div class="right-panel">

                <h2>Backup Scheduler</h2>
                
                <div class="schedule-form">
                    <h3>New Schedule</h3>
                    
                    <div class="form-group">
                        <label for="schedule-type">Type:</label>
                        <select id="schedule-type" class="form-control" onchange="showScheduleOptions()">
                            <!--<option value="once">One Time</option>-->
                            <option value="daily">Daily</option>
                            <option value="weekly">Weekly</option>
                            <option value="monthly">Monthly</option>
                            <option value="yearly">Yearly</option>
                        </select>
                    </div>
                    
                    <div class="form-group">
                        <label for="schedule-time">Hour:</label>
                        <input type="time" id="schedule-time" class="form-control" value="00:00">
                    </div>
                    
                    <!-- Opzioni specifiche per tipo -->
                    <div id="once-option" class="schedule-option">
                        <div class="form-group">
                            <label for="schedule-date">Data:</label>
                            <input type="date" id="schedule-date" class="form-control">
                        </div>
                    </div>
                    
                    <div id="weekly-option" class="schedule-option">
                        <div class="form-group">
                            <label for="schedule-day-week">Giorno settimana:</label>
                            <select id="schedule-day-week" class="form-control">
                                <option value="0">Monday</option>
                                <option value="1">Tuesday</option>
                                <option value="2">Wednesday</option>
                                <option value="3">Thursday</option>
                                <option value="4">Friday</option>
                                <option value="5">Saturday</option>
                                <option value="6">Sunday</option>
                            </select>
                        </div>
                    </div>
                    
                    <div id="monthly-option" class="schedule-option">
                        <div class="form-group">
                            <label for="schedule-day-month">Day month:</label>
                            <input type="number" id="schedule-day-month" min="1" max="31" value="1" class="form-control">
                        </div>
                    </div>
                    
                    <div id="yearly-option" class="schedule-option">
                        <div class="form-group">
                            <label for="schedule-month">Month:</label>
                            <select id="schedule-month" class="form-control">
                                <option value="1">January</option>
                                <option value="2">February</option>
                                <option value="3">March</option>
                                <option value="4">April</option>
                                <option value="5">May</option>
                                <option value="6">June</option>
                                <option value="7">July</option>
                                <option value="8">August</option>
                                <option value="9">September</option>
                                <option value="10">October</option>
                                <option value="11">November</option>
                                <option value="12">December</option>
                            </select>
                        </div>
                        <div class="form-group">
                            <label for="schedule-day-year">Day:</label>
                            <input type="number" id="schedule-day-year" min="1" max="31" value="1" class="form-control">
                        </div>
                    </div>
                    
                    <button onclick="addSchedule()" class="backup-btn">
                        <i class="fas fa-calendar-plus"></i> Add Schedule
                    </button>
                </div>
                
                <div class="schedule-list" id="schedules-list">
                    <!-- Lista delle pianificazioni verrà popolata dinamicamente -->
                </div>


            <div id="status-message" class="status"></div>

            <h2 style="margin-top: 30px;">Activity Log</h2>
            <div id="log" class="log">Ready to backup...</div>
            <button onclick="viewFullLog()" style="margin-top: 10px; width: 100%;">
                <i class="fas fa-scroll"></i> Show log
            </button>
        </div>
    </div>

	<div id="backup-modal" class="modal">
	    <div class="modal-content" style="width: 90%; max-width: 1400px; height: 85vh;">
		<div class="modal-header">
		    <div class="modal-title">Available backup by <span id="modal-switch-name"></span></div>
		    <span class="close-btn" onclick="closeModal()">&times;</span>
		</div>
		<div class="modal-body" style="height: calc(100% - 60px);">
		    <!-- Lista backup più stretta (25%) -->
		    <div class="backup-list" id="backup-list" style="width: 25%; min-width: 220px; border-right: 1px solid #eee; padding-right: 15px;">
		        <!-- Lista dei backup -->
		    </div>
		    
		    <!-- Contenuto principale più ampio (75%) -->
		    <div class="backup-content" style="width: 75%;">
		        <div id="backup-content-placeholder">
		            <i class="fas fa-arrow-left" style="font-size: 24px; color: #ccc; margin-bottom: 10px;"></i>
		            <p>Select a backup from the list to view content</p>
		        </div>
		        
		        <div id="backup-content" style="display: none; flex-direction: column; height: 100%;">
		            <!-- Barra dei bottoni compatta -->
		            <div style="display: flex; justify-content: flex-end; gap: 8px; padding: 8px 0; margin-bottom: 12px;">
		                <button class="action-btn delete-btn" id="delete-backup-btn" style="padding: 5px 12px; font-size: 13px;">
		                    <i class="fas fa-trash"></i> Delete
		                </button>
		                <button class="action-btn backup-btn" id="export-backup-btn" style="padding: 5px 12px; font-size: 13px;">
		                    <i class="fas fa-download"></i> Export
		                </button>
		            </div>
		            
		            <!-- Contenuto configurazione -->
		            <div class="config-container" style="flex: 1; overflow-y: auto; background: #f8f8f8; border-radius: 4px; padding: 15px;">
		                <div class="copy-tooltip" onclick="copyToClipboard(this)" style="position: relative;">
		                    <span class="tooltiptext">Copy to clipboard</span>
		                    <pre class="config-content" style="white-space: pre-wrap; margin: 0; font-family: 'Courier New', monospace; font-size: 13px;"></pre>
		                </div>
		            </div>
		        </div>
		    </div>
		</div>
	    </div>
	</div>

	<!-- Modal per modificare switch -->
	<div id="edit-modal" class="modal">
	    <div class="modal-content" style="max-width: 600px;">
		<div class="modal-header">
		    <div class="modal-title">Modify Device</div>
		    <span class="close-btn" onclick="closeEditModal()">&times;</span>
		</div>
		<div class="modal-body">
		    <div class="split-view">
		        <div class="form-column">
		            <div class="form-group">
		                <label for="edit-hostname">Hostname:</label>
		                <input type="text" id="edit-hostname">
		            </div>
		            <div class="form-group">
		                <label for="edit-ip">IP Address:</label>
		                <input type="text" id="edit-ip">
		            </div>
		        </div>
		        <div class="form-column">
		            <div class="form-group">
		                <label for="edit-username">Username:</label>
		                <input type="text" id="edit-username">
		            </div>
		            <div class="form-group">
		                <label for="edit-password">Password:</label>
		                <input type="password" id="edit-password" placeholder="Leave blank to keep current">
		            </div>
		            <div class="form-group">
		                <label for="edit-enable-password">Enable Password:</label>
		                <input type="password" id="edit-enable-password" placeholder="Leave blank to keep current">
		            </div>
		        </div>
		    </div>
		</div>
		<div style="text-align: right; margin-top: 20px;">
		    <button onclick="closeEditModal()">Cancel</button>
		    <button class="backup-btn" onclick="saveEditedSwitch()" style="margin-left: 10px;">Save changes</button>
		</div>
		<input type="hidden" id="edit-index">
	    </div>
	</div>

    <!-- Modal per visualizzare il log completo -->
    <div id="log-modal" class="modal">
        <div class="log-modal-content">
            <div class="modal-header">
                <div class="modal-title">Complete log activity</div>
                <span class="close-btn" onclick="closeLogModal()">&times;</span>
            </div>
            <div class="modal-body">
                <div id="full-log-content" class="log-content"></div>
            </div>
            <div style="text-align: right; margin-top: 20px;">
                <button onclick="closeLogModal()">Close</button>
            </div>
        </div>
    </div>

            <footer style="font-size: 12px; color: #777; text-align: center;">
                <br/>PICKLED v1.0.6
            </footer>

    <script src="https://cdnjs.cloudflare.com/ajax/libs/select2/4.0.13/js/select2.min.js"></script>
    <script>
        let switchesPerPage = 15;
        let currentSortColumn = -1;
        let sortDirection = 1;
        let currentPage = 1;
        let allSwitches = [];
        let filteredSwitches = [];

	window.eval = function() {
	    throw new Error("eval() is disabled for security reasons");
	};
	// Funzione di utilità per escape HTML
	function escapeHtml(unsafe) {
	    return unsafe
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;")
		.replace(/'/g, "&#039;");
	}
        function changeItemsPerPage() {
            const select = document.getElementById('items-per-page');
            switchesPerPage = parseInt(select.value);
            currentPage = 1; // Resetta alla prima pagina quando cambi il numero di elementi
            renderSwitches();
            updatePagination();
        }

	function safeInsert(text) {
	    return text.replace(/&/g, '&amp;')
		       .replace(/</g, '&lt;')
		       .replace(/>/g, '&gt;')
		       .replace(/"/g, '&quot;')
		       .replace(/'/g, '&#39;');
	}

	function filterSwitches() {
	    const searchTerm = document.getElementById('search-input').value.toLowerCase();
	    
	    if (searchTerm.trim() === '') {
		filteredSwitches = [...allSwitches];
	    } else {
		filteredSwitches = allSwitches.filter(sw => 
		    sw.hostname.toLowerCase().includes(searchTerm) || 
		    sw.ip.toLowerCase().includes(searchTerm) ||
		    (sw.username && sw.username.toLowerCase().includes(searchTerm))
		);
	    }
	    
	    currentPage = 1; // Resetta alla prima pagina quando filtri
	    renderSwitches();
	    updatePagination();
	}
        

	function renderSwitches() {
	    const tbody = document.getElementById('switches-table-body');
	    tbody.innerHTML = '';

	    if (filteredSwitches.length === 0) {
		tbody.innerHTML = '<tr><td colspan="4" style="text-align: center;">No matching devices found</td></tr>';
		return;
	    }

	    const startIndex = (currentPage - 1) * switchesPerPage;
	    const endIndex = Math.min(startIndex + switchesPerPage, filteredSwitches.length);
	    const switchesToShow = filteredSwitches.slice(startIndex, endIndex);

	    switchesToShow.forEach((sw, i) => {
		const row = document.createElement('tr');
		row.innerHTML = `
		    <td>${highlightMatches(sw.hostname)}</td>
		    <td>${highlightMatches(sw.ip)}</td>
		    <td>${highlightMatches(sw.username)}</td>
		    <td>
		        <button class="action-btn backup-btn" title="Backup" onclick="backupSwitch(${sw.originalIndex})">
		            <i class="fas fa-download"></i>
		        </button>
		        <button class="action-btn edit-btn" title="Modifica" onclick="openEditModal(${sw.originalIndex})">
		            <i class="fas fa-edit"></i>
		        </button>
		        <button class="action-btn view-btn" title="Visualizza Backup" onclick="viewBackups(${sw.originalIndex})">
		            <i class="fas fa-eye"></i>
		        </button>
		        <button class="action-btn delete-btn" title="Elimina" onclick="deleteSwitch(${sw.originalIndex})">
		            <i class="fas fa-trash"></i>
		        </button>
		    </td>
		`;
		tbody.appendChild(row);
	    });
	}
	
	function highlightMatches(text) {
	    if (!text) return ''; // Aggiunto controllo per valori null/undefined
	    const searchTerm = document.getElementById('search-input').value.toLowerCase();
	    if (!searchTerm || !text) return text;
	    
	    const str = text.toString();
	    const lowerStr = str.toLowerCase();
	    const termLower = searchTerm.toLowerCase();
	    
	    let result = '';
	    let lastIndex = 0;
	    let index = lowerStr.indexOf(termLower);
	    
	    while (index >= 0) {
		result += str.substring(lastIndex, index) + 
		         '<span style="background-color: yellow;">' + 
		         str.substring(index, index + searchTerm.length) + 
		         '</span>';
		lastIndex = index + searchTerm.length;
		index = lowerStr.indexOf(termLower, lastIndex);
	    }
	    
	    result += str.substring(lastIndex);
	    return result;
	}
        
	function updatePagination() {
            const pageCount = Math.ceil(filteredSwitches.length / switchesPerPage);
            const paginationDiv = document.getElementById('pagination');
            paginationDiv.innerHTML = '';

            // Aggiorna la selezione nel menu a tendina
            document.getElementById('items-per-page').value = switchesPerPage;
            
	    // Funzione per pulire tutti gli stati attivi
	    const clearActiveStates = () => {
		document.querySelectorAll('#pagination button').forEach(b => {
		    b.classList.remove('active');
		    b.disabled = false;
		});
	    };

	    // Pulsante "Indietro"
	    if (currentPage > 1) {
		const prevBtn = document.createElement('button');
		prevBtn.innerHTML = '<i class="fas fa-chevron-left"></i>';
		prevBtn.onclick = () => {
		    clearActiveStates();
		    currentPage--;
		    renderSwitches();
		};
		paginationDiv.appendChild(prevBtn);
	    }

	    // Pulsanti numerici
	    for (let i = 1; i <= pageCount; i++) {
		const btn = document.createElement('button');
		btn.textContent = i;
		btn.onclick = () => {
		    clearActiveStates();
		    currentPage = i;
		    btn.classList.add('active');
		    btn.disabled = true;
		    renderSwitches();
		};

		if (i === currentPage) {
		    btn.classList.add('active');
		    btn.disabled = true;
		}

		paginationDiv.appendChild(btn);
	    }

	    // Pulsante "Avanti"
	    if (currentPage < pageCount) {
		const nextBtn = document.createElement('button');
		nextBtn.innerHTML = '<i class="fas fa-chevron-right"></i>';
		nextBtn.onclick = () => {
		    clearActiveStates();
		    currentPage++;
		    renderSwitches();
		};
		paginationDiv.appendChild(nextBtn);
	    }

	    if (pageCount === 0) {
		paginationDiv.innerHTML = '<span>No devices found</span>';
	    }
	}

	function addSwitch() {
	    const hostname = document.getElementById('hostname').value;
	    const ip = document.getElementById('ip').value;
	    const username = document.getElementById('username').value;
	    const password = document.getElementById('password').value;
	    const enablePassword = document.getElementById('enable-password').value;
	    const deviceType = document.getElementById('device-type').value;

	    if (!hostname || !ip || !username || !password) {
		showStatus('Please fill all required fields', 'error');
		return;
	    }

	    const switchData = { 
		hostname, 
		ip, 
		username, 
		password,
		device_type: deviceType
	    };
	    
	    if (enablePassword) {
		switchData.enable_password = enablePassword;
	    }
            
            fetch('/add_switch', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCSRFToken()
                },
                body: JSON.stringify(switchData),
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    updateSwitchTable();
                    document.getElementById('hostname').value = '';
                    document.getElementById('ip').value = '';
                    document.getElementById('username').value = '';
                    document.getElementById('password').value = '';
                    showStatus('Switch aggiunto con successo', 'success');
                    addToLog(`Device ${hostname} (${ip}) added to the list`);
                } else {
                    showStatus('Error: ' + data.message, 'error');
                }
            })
            .catch(error => {
                showStatus('Connection error: ' + error, 'error');
            });
        }

        function uploadCSV() {
            const fileInput = document.getElementById('csv-file');
            const file = fileInput.files[0];
            
            if (!file) {
                showStatus('Select a CSV file to load', 'error');
                return;
            }
            
            const formData = new FormData();
            formData.append('csv_file', file);
            
            fetch('/upload_csv', {
                method: 'POST',
                body: formData,
                headers: {
                    'X-CSRFToken': getCSRFToken()
                }
            })
            .then(response => {
                if (!response.ok) {
                    throw new Error('Network response was not ok');
                }
                return response.json();
            })
            .then(data => {
                if (data.success) {
                    showStatus(`Loaded ${data.added} devices from CSV (${data.skipped} already in the list)`, 'success');
                    addToLog(`Loaded ${data.added} devices from CSV file`);
                    updateSwitchTable();
                    fileInput.value = '';
                } else {
                    showStatus('Error: ' + data.message, 'error');
                    addToLog(`CSV load failed: ${data.message}`);
                }
            })
            .catch(error => {
                showStatus('Error: ' + error.message, 'error');
                addToLog(`Error during CSV load: ${error.message}`);
            });
        }

	function sortTable(columnIndex) {
	    if (currentSortColumn === columnIndex) {
		sortDirection *= -1;
	    } else {
		currentSortColumn = columnIndex;
		sortDirection = 1;
	    }
	    
	    // Ordina filteredSwitches invece di fare una nuova richiesta
	    filteredSwitches.sort((a, b) => {
		const keys = ['hostname', 'ip', 'username'];
		const key = keys[columnIndex];
		const valA = a[key]?.toLowerCase() || '';
		const valB = b[key]?.toLowerCase() || '';
		
		if (valA < valB) return -1 * sortDirection;
		if (valA > valB) return 1 * sortDirection;
		return 0;
	    });
	    
	    currentPage = 1; // Resetta alla prima pagina quando si ordina
	    renderSwitches();
	    updatePagination();
	    updateSortIcons();
	}

	function updateSwitchTable() {
	    fetch('/get_switches')
	    .then(response => response.json())
	    .then(switchesData => {
		allSwitches = switchesData.map((sw, index) => ({...sw, originalIndex: index}));
		filteredSwitches = [...allSwitches]; // Inizializza con tutti gli switch
		
		renderSwitches();
		updatePagination();
		updateSortIcons();
	    })
	    .catch(error => {
		console.error('Device load failed:', error);
		const tbody = document.getElementById('switches-table-body');
		tbody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: red;">Error during the device update</td></tr>';
	    });
	}

        function updateSortIcons() {
            const headers = document.querySelectorAll('.switch-table th');
            headers.forEach((header, index) => {
                const icon = header.querySelector('i');
                if (icon) {
                    if (index === currentSortColumn) {
                        icon.className = sortDirection === 1 ? 'fas fa-sort-up' : 'fas fa-sort-down';
                    } else {
                        icon.className = 'fas fa-sort';
                    }
                }
            });
        }

	function deleteSwitch(index) {
	    fetch('/get_switches')
	    .then(response => response.json())
	    .then(switchesData => {
		if (index >= 0 && index < switchesData.length) {
		    const hostname = switchesData[index].hostname;
		    
		    if (!confirm(`Are you sure you wanna deleted the device ${hostname}?`)) {
		        return;
		    }

		    fetch('/delete_switch', {
		        method: 'POST',
		        headers: {
		            'Content-Type': 'application/json',
		            'X-CSRFToken': getCSRFToken()
		        },
		        body: JSON.stringify({ index: index }),
		    })
		    .then(response => response.json())
		    .then(data => {
		        if (data.success) {
		            updateSwitchTable();
		            updateSchedulesList();
		            showStatus(`Device ${hostname} deleted successfully`, 'success');
		            addToLog(`Device ${hostname} removed from list`);
		        } else {
		            showStatus('Error: ' + data.message, 'error');
		            addToLog(`ERROR - device delete failed ${hostname}: ${data.message}`);
		        }
		    })
		    .catch(error => {
		        showStatus('Connection error: ' + error, 'error');
		        addToLog(`ERROR - device delete failed: ${error}`);
		    });
		}
	    });
	}

	function backupSwitch(index) {
	    // Prima recuperiamo i dati dello switch per ottenere l'hostname
	    fetch('/get_switches')
	    .then(response => response.json())
	    .then(switchesData => {
		if (index >= 0 && index < switchesData.length) {
		    const switchData = switchesData[index];
		    const statusMessage = `Starting backup for ${switchData.hostname} (${switchData.ip})...`;
		    showStatus(statusMessage, 'success');
		    addToLog(statusMessage);
		    
		    // Poi eseguiamo il backup
		    fetch('/backup_switch', {
		        method: 'POST',
		        headers: {
		            'Content-Type': 'application/json',
		            'X-CSRFToken': getCSRFToken()
		        },
		        body: JSON.stringify({ index: parseInt(index) }),
		    })
		    .then(response => response.json())
		    .then(data => {
		        if (data.success) {
		            const successMessage = `Backup completed for ${data.hostname}`;
		            showStatus(successMessage, 'success');
		            addToLog(successMessage);
		            addToLog(`Config saved at: ${data.filename}`);
		        } else {
		            const errorMessage = `Backup error for ${switchData.hostname}: ${data.message}`;
		            showStatus(errorMessage, 'error');
		            addToLog(`ERROR - backup failed for ${switchData.hostname}: ${data.message}`);
		        }
		    })
		    .catch(error => {
		        const errorMessage = `Connection error for ${switchData.hostname}: ${error}`;
		        showStatus(errorMessage, 'error');
		        addToLog(`ERROR - Connection failed for ${switchData.hostname}: ${error}`);
		    });
		}
	    })
	    .catch(error => {
		const errorMessage = `Error fetching switch data: ${error}`;
		showStatus(errorMessage, 'error');
		addToLog(`ERROR - Failed to get switch data: ${error}`);
	    });
	}

        function backupAllSwitches() {
            addToLog('Starting backup for all devices...');
            
            fetch('/backup_all_switches', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCSRFToken()
                },
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showStatus(`Backup completato per ${data.count} switch`, 'success');
                    data.results.forEach(result => {
                        if (result.success) {
                            addToLog(`Backup completato per ${result.hostname} (${result.ip})`);
                        } else {
                            addToLog(`ERROR during the backup of ${result.hostname}: ${result.message}`);
                        }
                    });
                } else {
                    showStatus('Error: ' + data.message, 'error');
                }
            })
            .catch(error => {
                showStatus('Connection error: ' + error, 'error');
            });
        }


	function backupAllSwitches() {
	    const statusMessage = 'Starting backup for all devices...';
	    showStatus(statusMessage, 'success');
	    addToLog(statusMessage);
	    
	    fetch('/backup_all_switches', {
		method: 'POST',
		headers: {
		    'Content-Type': 'application/json',
		    'X-CSRFToken': getCSRFToken()
		},
	    })
	    .then(response => response.json())
	    .then(data => {
		if (data.success) {
		    const successMessage = `Backup completed for ${data.count} devices`;
		    showStatus(successMessage, 'success');
		    data.results.forEach(result => {
		        if (result.success) {
		            addToLog(`Backup completed for ${result.hostname} (${result.ip})`);
		        } else {
		            addToLog(`ERROR during the backup of ${result.hostname}: ${result.message}`);
		        }
		    });
		} else {
		    const errorMessage = `Backup error: ${data.message}`;
		    showStatus(errorMessage, 'error');
		}
	    })
	    .catch(error => {
		const errorMessage = `Connection error: ${error}`;
		showStatus(errorMessage, 'error');
	    });
	}
        function viewBackups(index) {
            fetch('/get_switch_backups', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCSRFToken()
                },
                body: JSON.stringify({ index }),
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    const modal = document.getElementById('backup-modal');
                    const switchName = document.getElementById('modal-switch-name');
                    const backupList = document.getElementById('backup-list');
                    
                    switchName.textContent = data.hostname;
                    backupList.innerHTML = '';
                    
                    if (data.backups.length === 0) {
                        backupList.innerHTML = '<p>No avalaible backup</p>';
                    } else {
                        data.backups.forEach(backup => {
                            const backupItem = document.createElement('div');
                            backupItem.className = 'backup-item';
                            backupItem.textContent = backup.filename;
                            backupItem.onclick = () => loadBackupContent(backup.path, index);
                            backupList.appendChild(backupItem);
                        });
                    }
                    
                    document.getElementById('backup-content').style.display = 'none';
                    document.getElementById('backup-content-placeholder').style.display = 'block';
                    modal.style.display = 'block';
                } else {
                    showStatus('Error: ' + data.message, 'error');
                }
            });
        }
        
	function loadBackupContent(filepath, switchIndex) {
	    fetch('/get_backup_content', {
		method: 'POST',
		headers: {
		    'Content-Type': 'application/json',
		    'X-CSRFToken': getCSRFToken()
		},
		body: JSON.stringify({ filepath }),
	    })
	    .then(response => response.json())
	    .then(data => {
		if (data.success) {
		    const contentDiv = document.getElementById('backup-content');
		    const placeholder = document.getElementById('backup-content-placeholder');
		    const configContent = document.querySelector('#backup-content .config-content');
		    const deleteBtn = document.getElementById('delete-backup-btn');
		    const exportBtn = document.getElementById('export-backup-btn');
		    
		    configContent.textContent = data.content;
		    
		    contentDiv.style.display = 'flex';
		    placeholder.style.display = 'none';
		    deleteBtn.style.display = 'inline-block';
		    exportBtn.style.display = 'inline-block';
		    deleteBtn.setAttribute('data-filepath', filepath);
		    deleteBtn.setAttribute('data-switch-index', switchIndex);
		    
		    // Evidenzia il backup selezionato nella lista
		    document.querySelectorAll('.backup-item').forEach(item => {
		        item.classList.toggle('active', item.textContent.includes(data.filename));
		    });
		}
	    });
	}
	
	function exportBackup() {
	    const configContent = document.querySelector('#backup-content .config-content').textContent;
	    const filename = document.querySelector('.backup-item.active').textContent;
	    
	    // Crea un blob con il contenuto
	    const blob = new Blob([configContent], { type: 'text/plain' });
	    const url = URL.createObjectURL(blob);
	    
	    // Crea un link temporaneo e simula il click
	    const a = document.createElement('a');
	    a.href = url;
	    a.download = filename;
	    document.body.appendChild(a);
	    a.click();
	    
	    // Pulisci
	    document.body.removeChild(a);
	    URL.revokeObjectURL(url);
	    
	    showStatus('Backup exported successfully', 'success');
	    addToLog(`Exported backup: ${filename}`);
	}

	function deleteBackup() {
	    const currentBackupPath = document.getElementById('delete-backup-btn').getAttribute('data-filepath');
	    if (!currentBackupPath) return;

	    if (!confirm('Are you sure you want to delete this backup? This action cannot be undone.')) {
		return;
	    }

	    fetch('/delete_backup', {
		method: 'POST',
		headers: {
		    'Content-Type': 'application/json',
		    'X-CSRFToken': getCSRFToken()
		},
		body: JSON.stringify({ filepath: currentBackupPath }),
	    })
	    .then(response => response.json())
	    .then(data => {
		if (data.success) {
		    showStatus('Backup deleted successfully', 'success');
		    addToLog(`Deleted backup: ${currentBackupPath}`);
		    closeModal();
		    // Se stavi visualizzando i backup di uno switch specifico, potresti voler ricaricare la lista
		    const currentSwitchIndex = document.getElementById('delete-backup-btn').getAttribute('data-switch-index');
		    if (currentSwitchIndex) {
		        viewBackups(currentSwitchIndex);
		    }
		} else {
		    showStatus('Error: ' + data.message, 'error');
		}
	    })
	    .catch(error => {
		showStatus('Connection error: ' + error, 'error');
	    });
	}

	function copyToClipboard(element) {
	    const configContent = element.querySelector('pre').textContent;
	    navigator.clipboard.writeText(configContent).then(() => {
		const tooltip = element.querySelector('.tooltiptext');
		tooltip.textContent = '✓ Copied!';
		element.classList.add('copied');
		
		setTimeout(() => {
		    tooltip.textContent = 'Copy to clipboard';
		    element.classList.remove('copied');
		}, 2000);
	    }).catch(err => {
		console.error('Failed to copy: ', err);
		element.querySelector('.tooltiptext').textContent = '✗ Failed to copy!';
	    });
	}

	function closeModal() {
	    document.getElementById('backup-modal').style.display = 'none';
	    document.getElementById('delete-backup-btn').style.display = 'none';
	    document.getElementById('export-backup-btn').style.display = 'none';
	}

	function openEditModal(index) {
	    fetch('/get_switches')
	    .then(response => response.json())
	    .then(switchesData => {
		if (index >= 0 && index < switchesData.length) {
		    const switchData = switchesData[index];
		    
		    document.getElementById('edit-hostname').value = switchData.hostname;
		    document.getElementById('edit-ip').value = switchData.ip;
		    document.getElementById('edit-username').value = switchData.username;
		    document.getElementById('edit-password').value = '';
		    document.getElementById('edit-enable-password').value = '';
		    document.getElementById('edit-index').value = index;
		    
		    document.getElementById('edit-modal').style.display = 'block';
		}
	    });
	}

	function saveEditedSwitch() {
	    const index = document.getElementById('edit-index').value;
	    const hostname = document.getElementById('edit-hostname').value;
	    const ip = document.getElementById('edit-ip').value;
	    const username = document.getElementById('edit-username').value;
	    const password = document.getElementById('edit-password').value;
	    const enablePassword = document.getElementById('edit-enable-password').value;

	    if (!hostname || !ip || !username) {
		showStatus('Please fill all required fields', 'error');
		return;
	    }

	    const switchData = { 
		index: parseInt(index),
		hostname: hostname,
		ip: ip,
		username: username
	    };
	    
	    if (password) {
		switchData.password = password;
	    }
	    
	    if (enablePassword) {
		switchData.enable_password = enablePassword;
	    }
	    
	    fetch('/update_switch', {
		method: 'POST',
		headers: {
		    'Content-Type': 'application/json',
		    'X-CSRFToken': getCSRFToken()
		},
		body: JSON.stringify(switchData),
	    })
	    .then(response => response.json())
	    .then(data => {
		if (data.success) {
		    updateSwitchTable();
		    updateSchedulesList();
		    closeEditModal();
		    showStatus('Device data updated successfully', 'success');
		    addToLog(`Device ${hostname} (${ip}) data updated`);
		} else {
		    showStatus('Error: ' + data.message, 'error');
		}
	    });
	}

	function closeEditModal() {
	    document.getElementById('edit-modal').style.display = 'none';
	}

	function showStatus(message, type) {
	    const statusElement = document.getElementById('status-message');
	    statusElement.textContent = message;
	    statusElement.className = 'status ' + type;
	    statusElement.style.display = 'block';
	    
	    // Auto-hide after 5 seconds
	    setTimeout(() => {
		statusElement.style.display = 'none';
	    }, 5000);
	}

	function addToLog(message) {
	    const logElement = document.getElementById('log');
	    const timestamp = new Date().toLocaleTimeString();
	    const messageDiv = document.createElement('div');
	    messageDiv.textContent = `[${timestamp}] ${message}`;
	    
	    // Aggiunge classi in base al tipo di messaggio
	    if (message.includes('ERROR:')) {
		messageDiv.style.color = '#ff6b6b';
	    } else if (message.includes('Starting') || message.includes('Connected') || message.includes('Executing')) {
		messageDiv.style.color = '#51cf66';
	    } else if (message.includes('completed')) {
		messageDiv.style.color = '#339af0';
	    }
	    
	    logElement.insertBefore(messageDiv, logElement.firstChild);
	    logElement.scrollTop = 0;
	}

	function colorLogLine(line) {
	    const lowerLine = line.toLowerCase();
	    if (lowerLine.includes('error') || lowerLine.includes('failed')) {
		return `<div class="log-line error">${line}</div>`;
	    } else if (lowerLine.includes('warning')) {
		return `<div class="log-line warning">${line}</div>`;
	    } else if (lowerLine.includes('success') || lowerLine.includes('completed')) {
		return `<div class="log-line success">${line}</div>`;
	    }
	    return `<div class="log-line">${line}</div>`;
	}



	function viewFullLog() {
	    fetch('/get_full_log')
	    .then(response => response.json())
	    .then(data => {
		if (data.success) {
		    const modal = document.getElementById('log-modal');
		    const logContent = document.getElementById('full-log-content');
		    
		    // Pulisci e formatta il log
			const lines = data.log.match(/[^\\r\\n]+/g) || [];
			logContent.innerHTML = lines
			    .filter(line => line.trim())
			    .map(line => `<div class="log-line">${line}</div>`)
			    .join('');
		        
		    modal.style.display = 'block';
		} else {
		    showStatus('Error: ' + data.message, 'error');
		}
	    });
	}

        function closeLogModal() {
            document.getElementById('log-modal').style.display = 'none';
        }

	window.onclick = function(event) {
	    const backupModal = document.getElementById('backup-modal');
	    const editModal = document.getElementById('edit-modal');
	    const logModal = document.getElementById('log-modal');
	    
	    if (event.target === backupModal) {
		backupModal.style.display = 'none';
	    }
	    if (event.target === editModal) {
		editModal.style.display = 'none';
	    }
	    if (event.target === logModal) {
		logModal.style.display = 'none';
	    }
	}

        function showScheduleOptions() {
            const type = document.getElementById('schedule-type').value;
            document.querySelectorAll('.schedule-option').forEach(option => {
                option.classList.remove('active');
            });
            
            if (type === 'once') {
                document.getElementById('once-option').classList.add('active');
            } else if (type === 'weekly') {
                document.getElementById('weekly-option').classList.add('active');
            } else if (type === 'monthly') {
                document.getElementById('monthly-option').classList.add('active');
            } else if (type === 'yearly') {
                document.getElementById('yearly-option').classList.add('active');
            }
        }

        function addSchedule() {
            const type = document.getElementById('schedule-type').value;
            const time = document.getElementById('schedule-time').value;
            
            const scheduleData = {
                type: type,
                time: time,
                enabled: true
            };
            
            if (type === 'once') {
                const date = document.getElementById('schedule-date').value;
                if (!date) {
                    showStatus('Seleziona una data valida', 'error');
                    return;
                }
                scheduleData.date = date;
            } else if (type === 'weekly') {
                scheduleData.day_of_week = document.getElementById('schedule-day-week').value;
            } else if (type === 'monthly') {
                scheduleData.day = document.getElementById('schedule-day-month').value;
            } else if (type === 'yearly') {
                scheduleData.month = document.getElementById('schedule-month').value;
                scheduleData.day = document.getElementById('schedule-day-year').value;
            }
            
            fetch('/add_schedule', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCSRFToken()
                },
                body: JSON.stringify(scheduleData),
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showStatus('Pianificazione aggiunta con successo', 'success');
                    updateSchedulesList();
                } else {
                    showStatus('Error: ' + data.message, 'error');
                }
            });
        }

        function updateSchedulesList() {
            fetch('/get_schedules')
            .then(response => response.json())
            .then(schedules => {
                const list = document.getElementById('schedules-list');
                list.innerHTML = '';
                
                if (schedules.length === 0) {
                    list.innerHTML = '<p>No active schedule</p>';
                    return;
                }
                
                schedules.forEach(schedule => {
                    const item = document.createElement('div');
                    item.className = 'schedule-item';
                    
                    const description = getScheduleDescription(schedule);
                    
                    item.innerHTML = `
                        <div class="schedule-item-header">
                            <span>${description}</span>
                            <div class="schedule-item-actions">
                                <button class="action-btn ${schedule.enabled ? 'edit-btn' : 'backup-btn'}" 
                                    onclick="toggleSchedule('${schedule.id}', ${!schedule.enabled})">
                                    <i class="fas fa-${schedule.enabled ? 'pause' : 'play'}"></i>
                                </button>
                                <button class="action-btn delete-btn" onclick="deleteSchedule('${schedule.id}')">
                                    <i class="fas fa-trash"></i>
                                </button>
                            </div>
                        </div>
                        <div>Backup globale di tutti gli switch</div>
                        <div>Prossima esecuzione: ${schedule.next_run || 'N/A'}</div>
                    `;
                    
                    list.appendChild(item);
                });
            });
        }

        function toggleSchedule(scheduleId, enable) {
            fetch('/toggle_schedule', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCSRFToken()
                },
                body: JSON.stringify({ id: scheduleId, enabled: enable }),
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showStatus(`Pianificazione ${enable ? 'attivata' : 'disattivata'}`, 'success');
                    updateSchedulesList();
                } else {
                    showStatus('Error: ' + data.message, 'error');
                }
            });
        }

        function deleteSchedule(scheduleId) {
            if (!confirm('Sei sicuro di voler eliminare questa pianificazione?')) {
                return;
            }
            
            fetch('/delete_schedule', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCSRFToken()
                },
                body: JSON.stringify({ id: scheduleId }),
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showStatus('Pianificazione eliminata', 'success');
                    updateSchedulesList();
                } else {
                    showStatus('Error: ' + data.message, 'error');
                }
            });
        }

        function getScheduleDescription(schedule) {
            let desc = '';
            const time = schedule.time || '00:00';
            
            switch (schedule.type) {
                case 'once':
                    desc = `Una volta il ${schedule.date} alle ${time}`;
                    break;
                case 'daily':
                    desc = `Giornaliero alle ${time}`;
                    break;
                case 'weekly':
                    const days = ['Domenica', 'Lunedì', 'Martedì', 'Mercoledì', 'Giovedì', 'Venerdì', 'Sabato'];
                    desc = `Settimanale ogni ${days[parseInt(schedule.day_of_week)]} alle ${time}`;
                    break;
                case 'monthly':
                    desc = `Mensile il giorno ${schedule.day} alle ${time}`;
                    break;
                case 'yearly':
                    const months = ['Gennaio', 'Febbraio', 'Marzo', 'Aprile', 'Maggio', 'Giugno', 
                                  'Luglio', 'Agosto', 'Settembre', 'Ottobre', 'Novembre', 'Dicembre'];
                    desc = `Annuale il ${schedule.day} ${months[parseInt(schedule.month) - 1]} alle ${time}`;
                    break;
            }
            
            return desc;
        }
	function getCSRFToken() {
	    const name = 'csrf_token';
	    const cookies = document.cookie.split(';');
	    for (let cookie of cookies) {
		let [key, value] = cookie.trim().split('=');
		if (key === name) return decodeURIComponent(value);
	    }
	    return '';
	}

        function exportSwitchesToCSV() {
            fetch('/export_switches_csv')
            .then(response => response.blob())
            .then(blob => {
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = 'switches_backup_' + new Date().toISOString().slice(0, 10) + '.csv';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                window.URL.revokeObjectURL(url);
                
                showStatus('Esportazione CSV completata', 'success');
                addToLog('Esportata lista switch in formato CSV');
            });
        }

	document.addEventListener('DOMContentLoaded', function() {
	    updateSwitchTable();
	    showScheduleOptions();
	    updateSchedulesList();
	    
	    const today = new Date().toISOString().split('T')[0];
	    document.getElementById('schedule-date').min = today;
	    document.getElementById('schedule-date').value = today;
	    
	    // Aggiungi event listener per la ricerca
	    document.getElementById('search-input').addEventListener('input', filterSwitches);
	    document.getElementById('delete-backup-btn').addEventListener('click', deleteBackup);
	    document.getElementById('export-backup-btn').addEventListener('click', exportBackup);
	});
    </script>
</body>
</html>
"""

if __name__ == '__main__':
	app.run(host='0.0.0.0', port=5000, debug=False)
