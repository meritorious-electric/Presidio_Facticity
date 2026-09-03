Presidio Facticity Guide

1. What this tool is

This program automatically scans documents for names and prints them as a .csv file (Excel spreadsheet) prior to human review. It provides confidence score, acknowledges duplicate names, and flags any names which likely belong to minors. It does not provide any additional functionality beyond finding and listing names.

1.1. Why this tool exists

Adobe Acrobat does not have a function for finding names you don’t already know. It can find other PII, like SSNs and credit card numbers, but can’t find names.

Everlaw AI can find names you don’t know, but it will cost hundreds of dollars if you are searching across thousands of pages of documents. On top of that, it is closed source and its errors are more complex to figure out (hallucinated names, for instance). Presidio can be fully audited and its errors are shared with Everlaw AI in the basic categories of false negatives (names not recognized that should be) and false positives (non-names recognized as names). 

2. Limitations

This tool is not a substitute for human review. It does not redact anything on its own. It puts out a significant number of false positives which a human reviewer will need to cull, this is by design and intended to limit the number of false negatives on the tail end of the distribution. There may be false negatives for very short or rare names, such as non-Western names or names which also function as common nouns. Typos in the original text or OCR errors may result in a name not being caught. Only names of individuals will be found, not organizations. The possible minors recognition has not been tested as extensively as the name recognition, and should be treated as such.

2.1. Error Statistics

According to a pilot sample of names (n=130) in fictional medical and legal documents, the hit rate for all names was 96%. This is consistent across both native PDFs and OCR’d text. The remaining 4% are clustered among non-Western and rare names. It is the responsibility of the human reviewer to find these remaining 4%. 

Names with diacritics are handled poorly by OCR and have a hit rate of only 9%. This is an issue with OCR transcribing those names incorrectly, not this program. Mononyms (people with only one name, like Morrissey) will not be captured.

Suppressed names can still be true names. You must check these. The reason for suppressing them is to keep them out of the main, high confidence file and make them more easily sortable. 

3. Before you start: OCR scanned files

All PDFs of scanned documents must be run through Adobe Acrobat’s Scan & OCR function. Do not do this for native PDFs. This creates the text layer that the program will read in order to find names. If you fail to do this, the program will not work. You should be able to select/highlight text in the PDF after it is OCR’d. Do not sanitize the documents of metadata in Acrobat until the full redaction process is complete, or else they will not function with this program.


4. Prerequisite files

Before running this program for the first time, make sure you have:
    • Python installed.
    • The program saved on your local drive somewhere you can find it easily (e.g. Documents/NameAuditTool)
    • A folder of OCR’d PDFs (e.g. Documents/NameAuditTool/pdfs)
    • An internet connection, for one time setup only. After setup, the tool runs entirely offline and does not send any document contents anywhere. 
    • About 1GB of free disk space for the one-time download of a language database, plus any other space necessary for the pdfs, .csv file, and cache to run the program. 
    • Microsoft Excel (or similar) to open the .csv file. 

5. One-time setup
	1. Install Python
    • Go to python.org/downloads and download the latest version for Windows.
    • Run the installer. Important: on the first screen, check the box that says "Add python.exe to PATH" before clicking Install. If you skip this, the commands below won't work.
	2. Install the required components.
    • The GUI should prompt you for these and install them on first launch.

6. Running the program
	1. Gather your OCR’d or native PDFs into one folder, e.g. “Documents/NameAuditTool/pdfs”.
	2. Navigate to the respective folders.
	3. Run the scan. This may be slow, as the program reads every page of every PDF.
    • You can rename the .csv file whatever you wish.
    • This may take anywhere from a few minutes to over an hour, depending on the length of the documents and the strength of your computer.
    • If the computer turns off or the program otherwise stops unexpectedly and you want to return to scan the same documents, simply rerun the program and it will pick up scanning where it left off.
	4. Clear the cache.
    • You should clear the cache whenever you need to move on to a new file and do not want the previous scan’s contents cluttering the .csv output. 

6.1. Troubleshooting/Common Errors
1. “python is not recognized”: Reinstall python.
	2. The scan seems stuck: large documents may take a while to show progress, but if it’s been more than 15-20 minutes with no new messages on a small batch it’s worth stopping and rerunning the same command. 
	3. A PDF is missing from the results: check the manifest file for a note like “skipped_no_text_layer” which means the file wasn’t OCR’d or “skipped_stale_cache” which means the file changes since it was last scanned.

7. Reading the .csv files

The first column lists likely names.

The tier column tells you how confident the program is on whether a name is a name, which is a useful quick proxy for edge cases but is not determinative of whether they should be redacted.

The confidence column gives a numerical value to the tiers which is slightly more granular.

The possible minor column flags whether the name appeared alongside language that indicated the name belonged to a minor, such as “custody hearing” or “daughter”, and is overinclusive for safety purposes.

The locations column lists which PDF files and on which page the name appears.

The possible duplicate column flags whether a name appears in two forms, like “Thomas Dewey” and “T. Dewey”.

The recognizer column tells you whether the name was detected by natural language (“spacy”), census data (“gazetteer”) or both (“both”).

The minor tier column gives a confidence rating to how likely a name is associated with a minor. Minor binding and minor reason give you the reasoning. The former is mostly for documentation, but the latter should be legible just by the output.

The suppressed .csv lists all items the program recognized as ambiguous. The bulk of these are organizations (“Fabian Society”), medical terms (“Klinfelter Syndrome”) and OCR garbage (“[&jad?/”). You should still read this file for true names which might be suppressed (“Frank Church”). You can sort these using Excel. 

8. Human review

	(1) Check every row and cull any outputs which are clearly not the names of individuals, such as “Jones Barbecue”. Begin your review with those names needing extensive review, then light, then the near-certain names. Prioritize names flagged as possible minors.

	(2) Search for the remaining names in Acrobat to identify whether they need to be redacted. Read them in context. The tier, confidence, and possible minor columns are proxies, not determinations. 

	(3) Log in the .csv file your determination as to whether a name needs to be redacted. This can be either “redact”, “keep”, or “uncertain”. 

	(4) Set aside a representative sample (~5-10%) of the documents for full human review. These should be full pages and chosen without any reference to this program’s outputs.

	(5) Based on the incidence rate of false negatives, where a name not caught by the program should have been redacted, make a determination as to the scope and extent of human review. This may range anywhere from skimming, to AI-assistance, to full human review. 

	(6) Have a second reviewer spot-check any uncertain determinations, if available. This will minimize human error from fatigue, miscalibration, or anything else.
