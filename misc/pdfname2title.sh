#!/bin/bash

# Set IFS since input has spaces, but first save old IFS
# See http://www.cyberciti.biz/tips/handling-filenames-with-spaces-in-bash.html
SAVEIFS=$IFS
IFS=$(echo -en "\n\b")

for i in $(ls *.pdf)
do
  # Rename the file so I can output to the original file name
  mv "$i" _"$i"
  # Save the file name
  filename=$(echo "$i" | sed 's/.pdf//')
  # Write an instructions file for pdftk
  echo "InfoKey: Title" > /tmp/pdftemp.txt
  echo "InfoValue: $filename" >> /tmp/pdftemp.txt
  # Change the PDF’s metadata, outputting to the original file name
  pdftk _"$i" update_info /tmp/pdftemp.txt output $i
  # Get rid of the renamed file
  rm _"$i"
done

# Restore IFS
IFS=$SAVEIFS