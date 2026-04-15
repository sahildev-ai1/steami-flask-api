# 🚀 Run Guide – steami-flask-api

This guide will help you set up and run the **steami-flask-api** project locally using a virtual environment, install dependencies, and access the Swagger UI.

---

## 📁 1. Navigate to Project Directory

```bash
cd steami-flask-api/
```

---

## 🐍 2. Create Virtual Environment

```bash
python3 -m venv venv
```

---

## ⚡ 3. Activate Virtual Environment

### On Linux / WSL / Mac:

```bash
source venv/bin/activate
```

### On Windows (PowerShell):

```powershell
venv\Scripts\activate
```

---

## 📦 4. Install Dependencies

Make sure you have a `requirements.txt` file in your project.

```bash
pip install -r requirements.txt
```

---

## ▶️ 5. Run the FastAPI Server

```bash
uvicorn main:app --host 0.0.0.0 --port 5000 --reload
```

---

## 🌐 6. Access the Application

### 🔹 Swagger UI (API Docs)

Open in your browser:

```
http://127.0.0.1:5000/docs
```

### 🔹 Alternative Docs (ReDoc)

```
http://127.0.0.1:5000/redoc
```

---

## 🧪 7. Test API

* Use Swagger UI to test endpoints interactively
* You can also use tools like:

  * Postman
  * cURL

---

## ⚠️ Common Issues & Fixes

### ❌ Swagger not opening?

* Ensure server is running
* Check correct port (5000)
* Try:

```bash
curl http://127.0.0.1:5000/docs
```

---

### ❌ Module not found error?

```bash
pip install -r requirements.txt
```

---

### ❌ Port already in use?

```bash
uvicorn main:app --port 8000
```

---

## ✅ You're Ready!

Your FastAPI backend should now be running successfully 🎉

---
