# AssetPilot — Guide d'intégration SAP S/4HANA (avis de maintenance)

## 1. Objectif
Les TAGs **RCM** (rouges) et **Safety** (jaunes) émis par les opérateurs dans AssetPilot deviennent des **avis de maintenance SAP PM**. Ils sont créés **au nom du chef d'équipe** qui valide le TAG. Les chefs d'équipe disposent déjà d'une licence S/4HANA et créent déjà des avis.
Les TAGs AM (bleus) restent dans AssetPilot.

```
Opérateur (sans licence SAP) ──► TAG RCM / Safety dans AssetPilot
Chef d'équipe (licence S/4HANA) ──► « Valider et créer l'avis SAP »
SAP ──► N° d'avis, puis N° d'OT et statut ──► AssetPilot (étiquette, tableau des anomalies)
Avis clôturé dans SAP ──► TAG « Traité » dans AssetPilot
```

## 2. Les modes (menu Administration → Intégration SAP)

| Mode | Fonctionnement | Travail IT |
|---|---|---|
| **Fiori prérempli** (démarrage) | La création d'avis s'ouvre dans le Fiori de l'entreprise, préremplie. Le chef se connecte avec son identifiant SAP, enregistre, puis reporte le N° d'avis (et plus tard le N° d'OT) dans AssetPilot. | Confirmer le lien de l'application Fiori de création d'avis et ses paramètres |
| **API directe** (cible) | Le chef relie une fois son compte SAP (OAuth 2.0). L'avis est ensuite créé directement à son nom. Le N° d'OT et le statut reviennent automatiquement (synchronisation toutes les 15 min). | Voir § 4 |

## 3. Contenu de l'avis envoyé

| AssetPilot | Champ SAP (`API_MAINTNOTIFICATION` / `MaintenanceNotification`) |
|---|---|
| Type RCM / Safety | `NotificationType` (paramétrable, ex. M2 / M1) |
| N° TAG + description (40 car.) | `NotificationText` |
| Type d'anomalie, machine, organe, description, découvert par, confirmé par | `MaintNotificationLongText` |
| Référence machine (ex. 5001129) | `TechnicalObject` + `TechObjIsEquipOrFuncnlLoc` = `EAMS_EQUI` |
| Priorité RCM / Safety | `MaintPriority` |
| Utilisateur SAP du chef | `ReportedByUser` |
| Poste de travail, division (si renseignés) | `MainWorkCenter`, `MaintenancePlanningPlant` |

Champs relus par la synchronisation : `MaintenanceOrder` (N° d'OT) et `NotifProcessingPhase` (4 = avis clôturé → TAG « Traité »).
Les noms de champs sont à vérifier sur votre système : `…/API_MAINTNOTIFICATION/$metadata`.

## 4. Mode API directe — actions côté IT / Basis

1. **Activer le service OData** `API_MAINTNOTIFICATION` sur la passerelle (transaction `/IWFND/MAINT_SERVICE`). La passerelle est déjà en service puisque Fiori est utilisé.
2. **Créer un client OAuth 2.0** (transaction `SOAUTH2`) :
   - client : par exemple `ASSETPILOT` ;
   - type d'autorisation : *Authorization Code*, avec jeton de rafraîchissement ;
   - adresse de retour (*redirect URI*) : celle affichée dans la page « Intégration SAP » d'AssetPilot (`https://<adresse-assetpilot>/sap/callback`) ;
   - portée : celle du service `API_MAINTNOTIFICATION` (affichée dans `SOAUTH2`, à reporter dans AssetPilot).
3. **Autorisations des chefs d'équipe** : leurs rôles doivent permettre la création et la lecture d'avis PM via ce service (objets `S_SERVICE`, `I_QMEL`, `I_SWERK`… selon votre concept de sécurité). En général, ils les ont déjà pour la création d'avis.
4. **Accès réseau** : AssetPilot doit pouvoir joindre la passerelle SAP en HTTPS. Si AssetPilot est hébergé à l'extérieur du réseau de l'entreprise, prévoir un accès sécurisé : hébergement interne ou en DMZ, reverse proxy, ou SAP BTP avec le Cloud Connector.
5. **Tester sur le système de qualification (QAS)**, puis en production.
6. **Licences** : les avis sont créés par les chefs d'équipe avec leur propre utilisateur SAP. Faire confirmer ce schéma par le responsable des licences SAP.

## 5. Paramétrage dans AssetPilot
Menu **Administration → Intégration SAP** :
- mode, types d'avis, priorités, division, poste de travail ;
- adresse du Fiori Launchpad et modèle de lien de création d'avis (variables `{fiori}`, `{equipment}`, `{type}`, `{text}`, `{priority}`, `{tag}`) ;
- mode API : adresse de la passerelle, mandant (`sap-client`), client et secret OAuth, portée, vérification du certificat ;
- **utilisateur SAP de chaque chef d'équipe** : dans sa fiche utilisateur.

Sécurité :
- aucun mot de passe SAP n'est stocké ;
- AssetPilot conserve uniquement les jetons OAuth de chaque chef, révocables à tout moment (« délier », ou révocation côté SAP) ;
- tous les échanges sont tracés dans le **journal SAP**.

## 6. Mise en service
- Choisir le mode **Fiori** ou **API**, puis renseigner l'utilisateur SAP de chaque chef d'équipe.
