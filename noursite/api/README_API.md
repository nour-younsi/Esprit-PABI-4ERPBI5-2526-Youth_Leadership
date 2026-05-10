# API ML - Guide Rapide

## 1) Prerequis recommandes

Pour assurer la compatibilite avec les modeles `.pkl`, utilise Python **3.11** (ou 3.10/3.12).

Exemple de creation d'environnement virtuel Windows:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r api/requirements-api.txt
```

## 2) Lancer l'API

Depuis la racine du projet:

```powershell
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

## 3) Documentation Swagger

- Swagger UI: http://127.0.0.1:8000/docs
- OpenAPI JSON: http://127.0.0.1:8000/openapi.json

## 4) Endpoints principaux

- `GET /health`: verifier que l'API est active
- `GET /models`: lister les modeles disponibles et les features attendues
- `POST /predict/{model_key}`: prediction tabulaire (regression, classification, clustering)
- `POST /forecast/{model_key}`: prediction serie temporelle

## 5) Exemples de requetes

### 5.1 Regression (Notebook 4)

```powershell
curl -X POST "http://127.0.0.1:8000/predict/budget_notebook_4" ^
  -H "Content-Type: application/json" ^
  -d "{\"features\":{\"Nb_Activites_Prevu\":12,\"Nb_Adherents\":80,\"Activity_Per_Member\":0.15,\"Member_Cost_Ratio\":30,\"Activity_Cost_Ratio\":12,\"Budget_Efficiency\":1.1,\"Unite_enc\":2}}"
```

### 5.2 Regression (Notebook 5)

```powershell
curl -X POST "http://127.0.0.1:8000/predict/participation_notebook_5" ^
  -H "Content-Type: application/json" ^
  -d "{\"features\":{\"Duree_Jours\":3,\"Budget_Individuel\":65,\"Budget_Efficiency\":1.05,\"Duration_Scaled\":0.4,\"Activity_Intensity\":0.7,\"Engagement_Factor\":0.8,\"Type_Activite_enc\":1,\"Saison_enc\":2,\"Unite_enc\":0,\"Age_Category_enc\":1}}"
```

### 5.3 Clustering (Notebook 9)

```powershell
curl -X POST "http://127.0.0.1:8000/predict/segmentation_notebook_9" ^
  -H "Content-Type: application/json" ^
  -d "{\"features\":{\"Anciennete_Annees\":5,\"Nb_Presences_Mois\":6,\"Nb_Badges_Obtenus\":4,\"Cotisation_A_Jour\":1,\"Participation_Rate\":0.72,\"Engagement_Score\":0.78,\"Activity_Consistency\":0.7,\"Loyalty_Score\":0.75,\"Progression\":0.6,\"Motivation\":0.8}}"
```

### 5.4 Serie temporelle (SARIMA)

```powershell
curl -X POST "http://127.0.0.1:8000/forecast/timeseries_sarima" ^
  -H "Content-Type: application/json" ^
  -d "{\"periods\":12}"
```

## 6) Remarques importantes

- Les valeurs envoyees doivent respecter les features attendues par chaque modele.
- Pour les modeles avec `*_enc` dans les noms de features, envoie des valeurs numeriques deja encodees.
- Si tu veux, je peux ajouter une couche d'encodage automatique (string -> encodeur `.pkl`) dans une version 2.
