"""
Luna ML tool — machine learning and deep learning helper.
Reads JSON from stdin, prints result to stdout.
Actions: info (what libs are available), train (fit a small model), predict (run inference).
Uses scikit-learn by default; PyTorch used for 'nn' model type if available.
"""
import json
import os
import sys

def main():
    try:
        params = json.load(sys.stdin)
    except Exception as e:
        print(f"Error: Invalid JSON - {e}")
        return
    action = (params.get("action") or params.get("cmd") or params.get("query") or "info")
    if isinstance(action, str):
        action = action.strip().lower() or "info"
    else:
        action = "info"
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    models_dir = os.path.join(base_dir, "ml_models")
    os.makedirs(models_dir, exist_ok=True)

    if action == "info":
        out = {"ok": True, "message": "ML tool ready.", "libs": {}}
        try:
            import sklearn
            out["libs"]["sklearn"] = True
            out["sklearn_version"] = getattr(sklearn, "__version__", "?")
        except ImportError:
            out["libs"]["sklearn"] = False
        try:
            import torch
            out["libs"]["torch"] = True
            out["torch_version"] = getattr(torch, "__version__", "?")
        except ImportError:
            out["libs"]["torch"] = False
        if not out["libs"].get("sklearn") and not out["libs"].get("torch"):
            out["message"] = "Install scikit-learn for ML (pip install scikit-learn). PyTorch optional for deep learning."
        msg = out["message"]
        if out["libs"].get("sklearn"):
            msg += f" sklearn={out.get('sklearn_version', '?')}"
        if out["libs"].get("torch"):
            msg += f" torch={out.get('torch_version', '?')}"
        print(msg)
        return

    if action == "train":
        model_type = (params.get("model_type") or params.get("type") or "classifier").strip().lower()
        name = (params.get("name") or params.get("model_name") or "model").strip().replace(" ", "_")[:64]
        if not name.replace("_", "").isalnum():
            name = "model"
        X = params.get("X") or params.get("features") or params.get("data")
        y = params.get("y") or params.get("target") or params.get("labels")
        if X is None or y is None:
            print("Error: train needs X (features) and y (target) in the JSON params.")
            return
        if isinstance(X, list) and isinstance(y, list) and len(X) != len(y):
            print("Error: X and y must have same length.")
            return
        try:
            import numpy as np
            X = np.array(X)
            y = np.array(y)
        except Exception as e:
            print(f"Error: Convert to arrays failed: {e}")
            return
        if model_type in ("classifier", "classify", "clf"):
            try:
                from sklearn.ensemble import RandomForestClassifier
                from sklearn.model_selection import train_test_split
                model = RandomForestClassifier(n_estimators=20, max_depth=5, random_state=42)
                model.fit(X, y)
                path = os.path.join(models_dir, f"{name}.joblib")
                import joblib
                joblib.dump(model, path)
                print(f"Classifier saved as {name}. Ready for !ml predict.")
            except ImportError as e:
                print(f"Error: Need scikit-learn and joblib. pip install scikit-learn joblib")
            except Exception as e:
                print(f"Error: {e}")
        elif model_type in ("regressor", "regress", "reg"):
            try:
                from sklearn.ensemble import RandomForestRegressor
                import joblib
                model = RandomForestRegressor(n_estimators=20, max_depth=5, random_state=42)
                model.fit(X, y)
                path = os.path.join(models_dir, f"{name}.joblib")
                joblib.dump(model, path)
                print(f"Regressor saved as {name}. Ready for !ml predict.")
            except ImportError as e:
                print(f"Error: Need scikit-learn and joblib. pip install scikit-learn joblib")
            except Exception as e:
                print(f"Error: {e}")
        else:
            print(f"Error: model_type must be classifier or regressor, got {model_type}")
        return

    if action == "predict":
        name = (params.get("name") or params.get("model_name") or "model").strip().replace(" ", "_")[:64]
        features = params.get("features") or params.get("X") or params.get("data")
        if features is None:
            print("Error: predict needs 'features' (list or list of lists).")
            return
        path = os.path.join(models_dir, f"{name}.joblib")
        if not os.path.isfile(path):
            print(f"Error: Model {name} not found. Train first with action=train, name={name}")
            return
        try:
            import numpy as np
            import joblib
            X = np.array(features)
            if X.ndim == 1:
                X = X.reshape(1, -1)
            model = joblib.load(path)
            pred = model.predict(X)
            result = pred.tolist() if hasattr(pred, "tolist") else list(pred)
            print(f"Predictions: {result}")
        except ImportError:
            print("Error: Need numpy and joblib. pip install numpy joblib scikit-learn")
        except Exception as e:
            print(f"Error: {e}")
        return

    if action == "list":
        try:
            names = [f[:-7] for f in os.listdir(models_dir) if f.endswith(".joblib")]
            print(f"Models: {', '.join(names) or '(none)'}")
        except Exception as e:
            print(f"Error: {e}")
        return

    print(f"Error: Unknown action '{action}'. Use info, train, predict, or list.")

if __name__ == "__main__":
    main()
