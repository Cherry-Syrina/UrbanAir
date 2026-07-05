# 03_SystemArchitecture.md

# UrbanAir AI – System Architecture

---

# Overview

UrbanAir AI follows a modular and scalable architecture that combines geospatial intelligence, machine learning, environmental monitoring, and Large Language Models (LLMs) to help city administrators make proactive decisions for air quality management.

The architecture is divided into multiple layers:

- Data Layer
- Data Ingestion Layer
- Processing Layer
- AI Intelligence Layer
- Backend Services
- Frontend Dashboard
- Notification Layer

Each layer has a dedicated responsibility, making the platform scalable and easy to maintain.

---

# Overall Architecture

> **📌 Insert High-Level Architecture Diagram Here**

```
<img width="1536" height="1024" alt="image" src="https://github.com/user-attachments/assets/9957b11a-628b-4126-9d17-d9c2acc7be84" />

```

---

## Architecture Layers

```
Data Sources
      │
      ▼
Data Collection
      │
      ▼
Data Processing
      │
      ▼
Database
      │
      ▼
AI Intelligence Layer
      │
      ▼
FastAPI Backend
      │
      ▼
React Dashboard
      │
      ▼
Officials & Citizens
```

---

# Layer 1 — Data Sources

UrbanAir AI collects data from multiple environmental sources.

## Data Sources

- CPCB Air Quality Stations (CAAQMS)
- Weather APIs
- Satellite Images
- Traffic Data
- Land Use Maps
- Population Data
- Hospital Locations
- Construction Zones

These datasets are collected periodically to keep the system updated.

---

# Layer 2 — Data Ingestion

The Data Ingestion Layer fetches information from different APIs and datasets.

## Components

- AQI Collector
- Weather Collector
- Traffic Collector
- Satellite Collector
- Land Use Loader

## Responsibilities

- Fetch latest data
- Validate data
- Remove duplicates
- Store raw information

---

# Layer 3 — Data Processing

Raw environmental data is transformed into machine-learning-ready features.

Processing includes:

- Missing value handling
- Data normalization
- Timestamp alignment
- Spatial mapping
- Feature engineering

Output:

- Clean AQI Dataset
- Weather Dataset
- Traffic Dataset
- Satellite Dataset

---

# Layer 4 — Database

The processed datasets are stored for prediction and visualization.

## Database Components

- AQI Database
- Weather Database
- Prediction Database
- Historical Records
- User Queries

Suggested Database

- PostgreSQL

Optional

- Redis Cache

---

# Layer 5 — AI Intelligence Layer

This is the core of UrbanAir AI.

Multiple AI modules work together to generate intelligent recommendations.

Modules include

- AQI Forecast Engine
- Pollution Source Attribution Engine
- Enforcement Intelligence Engine
- Health Advisory Engine
- AI Chat Assistant
- What-if Simulation Engine

---

# AI Workflow

> **📌 Insert AI Agent Workflow Diagram Here**

```
<img width="1024" height="1536" alt="image" src="https://github.com/user-attachments/assets/bd1fd2ca-2471-454b-9fd2-1e340f4323c4" />

```

---

## AI Modules

### AQI Forecast Engine

Purpose

Predict AQI for

- 24 Hours
- 48 Hours
- 72 Hours

Possible Algorithms

- XGBoost
- LightGBM
- Random Forest

---

### Pollution Source Attribution Engine

Purpose

Estimate pollution contribution from

- Traffic
- Construction
- Industries
- Waste Burning

Outputs

```
Traffic        45%

Construction   28%

Industry       18%

Others          9%
```

---

### Enforcement Intelligence Engine

Purpose

Prioritize inspection areas.

Example

```
Priority Area

Anand Vihar

Reason

Heavy Traffic

Construction Dust

Low Wind Speed

Recommendation

Deploy Inspection Team
```

---

### Health Advisory Engine

Purpose

Generate health recommendations for citizens.

Example

```
AQI Tomorrow

295

Advice

Wear N95

Avoid Outdoor Exercise

Schools Stay Indoor
```

---

### AI Chat Assistant

Powered By

- Gemini API

Capabilities

- Explain AQI increase
- Show pollution sources
- Answer administrator questions
- Generate natural language insights

---

### What-if Simulation Engine

Purpose

Estimate impact before implementing an intervention.

Example

```
Reduce Construction by 50%

↓

AQI

280

↓

235

↓

Improvement

16%
```

---

# Component-Level Architecture

> **📌 Insert Component-Level Architecture Diagram Here**

```
<img width="1536" height="1024" alt="image" src="https://github.com/user-attachments/assets/0d5f4c3f-0b53-4a1b-8f43-4cba2ab1177b" />

```

---

## Component Specifications

| Component | Technology | Input | Output | Purpose |
|------------|------------|--------|---------|----------|
| AQI Collector | Python | CPCB API | AQI Data | Collect AQI |
| Weather Collector | Python | Weather API | Weather Data | Collect Weather |
| Traffic Collector | Python | Traffic API | Traffic Data | Collect Traffic |
| Data Processing | Pandas | Raw Data | Clean Data | Preprocessing |
| Forecast Engine | XGBoost | AQI + Weather | AQI Prediction | Forecast AQI |
| Source Attribution | ML + Rule Engine | AQI + Traffic | Pollution Sources | Source Detection |
| Enforcement Engine | Rule Engine | Forecast | Recommendations | Inspection Planning |
| Health Advisory | Python | AQI + Population | Health Alerts | Citizen Safety |
| Chat Assistant | Gemini API | User Query | AI Response | Conversational AI |
| Backend API | FastAPI | Requests | JSON | API Layer |
| Dashboard | React | JSON | UI | Visualization |

---

# Data Flow

The following diagram illustrates the complete movement of data inside UrbanAir AI.

> **📌 Insert Data Flow Diagram Here**

```
<img width="1024" height="1536" alt="image" src="https://github.com/user-attachments/assets/18f50b59-8696-44b5-bbeb-38dd20d8c167" />

```

---

## Data Flow Steps

1. Collect AQI data from CPCB.
2. Collect weather information.
3. Fetch satellite imagery.
4. Fetch traffic information.
5. Clean and preprocess data.
6. Store processed datasets.
7. Run AI prediction models.
8. Generate AQI forecast.
9. Identify pollution sources.
10. Generate enforcement recommendations.
11. Generate health advisories.
12. Display results on dashboard.

---

# Backend Architecture

Backend Framework

- FastAPI

Responsibilities

- REST APIs
- AI Model Integration
- Database Access
- Authentication
- Dashboard APIs

---

# Frontend Architecture

Frontend Framework

- React
- Tailwind CSS
- Leaflet
- Recharts

Modules

- Dashboard
- AQI Map
- Forecast Screen
- Source Attribution
- Enforcement Panel
- Health Advisory
- AI Chat

---

# Technology Stack

## Frontend

- React
- Tailwind CSS
- Leaflet
- Recharts

## Backend

- FastAPI

## AI

- Python
- Scikit-learn
- XGBoost
- LightGBM

## GIS

- GeoPandas
- OpenStreetMap

## Database

- PostgreSQL

## LLM

- Gemini API

---

# Advantages of the Architecture

- Modular Design
- Scalable
- Easy to Maintain
- Supports Multi-Agent AI
- Easy API Integration
- Supports Multiple Cities in Future
- Real-time Data Processing
- Explainable AI Recommendations

---

# Future Scalability

The architecture is designed to support:

- Multiple Cities
- IoT Sensor Networks
- Drone Monitoring
- Mobile Application
- Digital Twin
- Edge AI
- Real-time Streaming
- Smart Traffic Optimization

---

# Architecture Summary

UrbanAir AI follows a layered architecture where environmental data is collected, processed, analyzed using AI, and finally presented as actionable intelligence for city administrators. The modular design ensures scalability, maintainability, and future expansion while enabling proactive air quality management.
